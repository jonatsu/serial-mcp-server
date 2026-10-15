"""Port log: a per-connection recording of the bytes on the wire.

This is separate from the tool-call trace in ``mcp/trace.py`` and from the Python
``logging`` output. A ``PortLog`` owns a queue and one disk thread, so file
writes never block the reader or writer threads. Bounded admission includes
in-flight entries; losses are counted and recorded before later accepted entries.

No MCP imports. The script runner shares ``private_files`` with this module for creating its files.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from serial_mcp_server.portlog.admission import MetadataRejected, RetainedEntry, snapshot_metadata
from serial_mcp_server.portlog.formats import (
    CLOSE_EVENT,
    CONFIGURE_EVENT,
    FORMATS,
    KIND_EVENT,
    KIND_OVERFLOW,
    KIND_RX,
    KIND_TX,
    LOG_ADMISSION_FAILED_EVENT,
    Entry,
    FormatWriter,
    WriterOptions,
)
from serial_mcp_server.portlog.request import LogRequest
from serial_mcp_server.private_files import DIR_MODE, MAX_NAME_COLLISIONS, log_base_name, open_exclusive

logger = logging.getLogger("serial_mcp_server")

# Payload retained by the queue and disk thread. Reject whole new entries beyond this existing cap,
# keeping serial reception responsive. Format-owned partial-line buffers are outside admission accounting.
QUEUE_LIMIT_BYTES = 8 * 1024 * 1024
# Empty payloads and events also consume resources. Reject new ordinary entries beyond this count or
# metadata budget, including the in-flight entry and ordinary overflow markers. These complement the
# payload cap without claiming an exact Python heap bound.
QUEUE_LIMIT_ENTRIES = 4096
QUEUE_LIMIT_METADATA_BYTES = 1024 * 1024
# How long ``close`` waits for the disk thread to write the queue and close the files. After that ``close``
# returns while the thread finishes on its own, so file sizes read right then can fall short.
CLOSE_JOIN_TIMEOUT_S = 5.0
# Log files are named ``<prefix>-<DDMMYYYY>-<HHMM>[-<n>]``; see ``private_files.MAX_NAME_COLLISIONS``.
LOG_FILE_PREFIX = "serial-mcp-log"

# ---------------------------------------------------------------------------
# PortLog
# ---------------------------------------------------------------------------


class PortLog:
    """Queue plus disk thread for one connection's log files.

    Admission never waits for disk I/O. Payload, entry, and metadata budgets include the current write.
    New ordinary entries are dropped whole under pressure; a loss marker precedes the next admitted entry.
    Rejection of an encoding-change event stops admission so later bytes cannot be decoded incorrectly.
    After closure or admission failure, taps are no-ops. Files have no size limit or rotation.
    """

    def __init__(
        self,
        writers: dict[str, FormatWriter],
        files: dict[str, Path],
        *,
        queue_limit: int = QUEUE_LIMIT_BYTES,
        entry_limit: int = QUEUE_LIMIT_ENTRIES,
        metadata_limit: int = QUEUE_LIMIT_METADATA_BYTES,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._writers = dict(writers)
        self.files = dict(files)
        self._queue_limit = queue_limit
        self._entry_limit = entry_limit
        self._metadata_limit = metadata_limit
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._start = monotonic()
        self._cond = threading.Condition()
        self._queue: deque[RetainedEntry] = deque()
        self._queued_bytes = 0
        self._retained_entries = 0
        self._retained_metadata = 0
        self._pending_bytes = 0
        self._pending_entries = 0
        self._pending_events = 0
        self._dropped_bytes = 0
        self._dropped_entries = 0
        self._dropped_events = 0
        self._admission_error: str | None = None
        self._entries = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="serial-portlog")
        self._thread.start()

    @classmethod
    def start(cls, request: LogRequest, encoding: str, now: datetime | None = None) -> PortLog:
        """Create the log files for *request* and start logging.

        Raises:
            OSError: The directory or files could not be created.
        """
        opened = open_log_files(request.directory, request.formats, now or datetime.now())
        options = WriterOptions(encoding, request.text_timestamps)
        writers = {name: FORMATS[name].make_writer(fh, options) for name, (_, fh) in opened.items()}
        return cls(writers, {name: path for name, (path, _) in opened.items()})

    def rx(self, data: bytes) -> None:
        self._put(KIND_RX, data=data)

    def tx(self, data: bytes, source: str) -> None:
        self._put(KIND_TX, data=data, source=source)

    def event(self, name: str, **fields: Any) -> bool:
        """Return whether the event was admitted; a close event ends admission exactly once."""
        return self._put(KIND_EVENT, name=name, fields=fields)

    def status(self) -> dict[str, Any]:
        """Report files, completed entries, loss counters, and a terminal admission failure if present.

        ``entries`` lags the queue and leaves out overflow markers.
        """
        with self._cond:
            return {
                "formats": list(self.files),
                "files": {name: str(path) for name, path in self.files.items()},
                "entries": self._entries,
                "dropped_bytes": self._dropped_bytes,
                "dropped_entries": self._dropped_entries,
                "dropped_events": self._dropped_events,
                "admission_error": self._admission_error,
            }

    def close(self) -> None:
        """Write what is queued, close the files, and stop accepting entries."""
        with self._cond:
            if not self._closed:
                self._finish(self._wall_clock(), self._offset())
        self._thread.join(timeout=CLOSE_JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            # The disk thread closes the files itself when it finishes, so nothing leaks.
            logger.warning("Port log disk thread did not finish within %.0f s.", CLOSE_JOIN_TIMEOUT_S)

    def discard(self) -> None:
        """Close a log that was never attached and remove its files."""
        self.close()
        for path in self.files.values():
            path.unlink(missing_ok=True)

    def _offset(self) -> float:
        """Seconds since the log started, on the monotonic clock."""
        return self._monotonic() - self._start

    def _put(
        self,
        kind: str,
        *,
        data: bytes = b"",
        source: str = "",
        name: str = "",
        fields: dict[str, Any] | None = None,
    ) -> bool:
        wall, offset = self._wall_clock(), self._offset()
        candidate = Entry(kind, wall, offset, data, source, name, fields or {})
        with self._cond:
            if self._closed:
                return False
            if kind == KIND_EVENT and name == CLOSE_EVENT:
                self._finish(wall, offset, name=CLOSE_EVENT)
                return True
            try:
                metadata = snapshot_metadata(source, name, fields or {})
            except MetadataRejected as exc:
                self._reject(candidate, str(exc))
                return False
            entry = RetainedEntry(kind, wall, offset, data, metadata)
            marker = self._overflow_entry(wall, offset) if self._pending_entries else None
            if not self._fits(entry, marker):
                self._reject(candidate, "retained admission budget exhausted")
                return False
            if marker is not None:
                self._append(marker)
                self._clear_pending()
            self._append(entry)
            self._cond.notify()
            return True

    def _fits(self, entry: RetainedEntry, marker: RetainedEntry | None) -> bool:
        count = 1 + (marker is not None)
        metadata = len(entry.metadata) + (len(marker.metadata) if marker is not None else 0)
        return (
            self._queued_bytes + len(entry.data) <= self._queue_limit
            and self._retained_entries + count <= self._entry_limit
            and self._retained_metadata + metadata <= self._metadata_limit
        )

    def _append(self, entry: RetainedEntry) -> None:
        self._queue.append(entry)
        if entry.ordinary:
            self._queued_bytes += len(entry.data)
            self._retained_entries += 1
            self._retained_metadata += len(entry.metadata)

    def _reject(self, entry: Entry, reason: str) -> None:
        self._pending_bytes += len(entry.data)
        self._pending_entries += 1
        self._pending_events += entry.kind == KIND_EVENT
        self._dropped_bytes += len(entry.data)
        self._dropped_entries += 1
        self._dropped_events += entry.kind == KIND_EVENT
        if entry.kind == KIND_EVENT and entry.name == CONFIGURE_EVENT and "encoding" in entry.fields:
            self._admission_error = (
                f"Encoding-change event was not retained: {reason}. Stop and restart logging."
            )
            self._finish(
                entry.wall, entry.offset, name=LOG_ADMISSION_FAILED_EVENT, reason=self._admission_error
            )
            logger.warning("Port log admission failed: %s", self._admission_error)

    def _overflow_entry(self, wall: datetime, offset: float, *, ordinary: bool = True) -> RetainedEntry:
        metadata = snapshot_metadata(
            "",
            "",
            {
                "dropped_bytes": self._pending_bytes,
                "dropped_entries": self._pending_entries,
                "dropped_events": self._pending_events,
            },
        )
        return RetainedEntry(KIND_OVERFLOW, wall, offset, b"", metadata, ordinary)

    def _clear_pending(self) -> None:
        self._pending_bytes = self._pending_entries = self._pending_events = 0

    def _finish(self, wall: datetime, offset: float, *, name: str = "", reason: str = "") -> None:
        # Only this terminal transition may bypass ordinary admission. It reserves at most two bounded
        # entries: one aggregate loss marker and one close/failure event. Repeated shutdown is a no-op.
        if self._pending_entries:
            self._append(self._overflow_entry(wall, offset, ordinary=False))
            self._clear_pending()
        if name:
            metadata = snapshot_metadata("", name, {"reason": reason} if reason else {})
            self._append(RetainedEntry(KIND_EVENT, wall, offset, b"", metadata, ordinary=False))
        self._closed = True
        self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                retained = self._queue.popleft() if self._queue else None
                if retained is None and self._closed:
                    break
            if retained is None:
                self._idle()
                continue
            entry = retained.decoded()
            self._for_each_writer(lambda writer, item=entry: writer.write(item))
            self._for_each_writer(lambda writer, offset=entry.offset: writer.tick(offset))
            with self._cond:
                if retained.ordinary:
                    self._queued_bytes -= len(retained.data)
                    self._retained_entries -= 1
                    self._retained_metadata -= len(retained.metadata)
                if entry.kind != KIND_OVERFLOW:
                    self._entries += 1
            # Release the completed payload and decoded snapshot before waiting for another entry.
            del entry, retained
        self._for_each_writer(lambda writer: writer.flush())
        self._for_each_writer(lambda writer: writer.close(), drop_on_error=False)

    def _idle(self) -> None:
        """Flush, then wait for an entry, a close, or the time a writer needs to act."""
        self._for_each_writer(lambda writer: writer.flush())  # make written data visible to file readers
        deadline = self._next_deadline()
        with self._cond:
            if not self._queue and not self._closed:
                timeout = None if deadline is None else max(0.0, deadline - (self._offset()))
                self._cond.wait(timeout)
        if deadline is not None:
            now = self._offset()
            self._for_each_writer(lambda writer: writer.tick(now))

    def _next_deadline(self) -> float | None:
        deadlines = [
            d for d in (writer.pending_deadline() for writer in self._writers.values()) if d is not None
        ]
        return min(deadlines) if deadlines else None

    def _for_each_writer(self, action: Callable[[FormatWriter], None], *, drop_on_error: bool = True) -> None:
        for name, writer in list(self._writers.items()):
            try:
                action(writer)
            except Exception:
                logger.warning("Port log format %r failed; it stops logging.", name, exc_info=True)
                if drop_on_error:
                    del self._writers[name]
                    self._close_quietly(writer)

    @staticmethod
    def _close_quietly(writer: FormatWriter) -> None:
        try:
            writer.close()
        except Exception:
            logger.debug("Closing a failed port log file also failed.", exc_info=True)


class PortLogSlot:
    """Holds a connection's current port log so it can be swapped while threads run.

    The reader and writer threads call the taps below; with no log set they do nothing.
    """

    def __init__(self) -> None:
        self._log: PortLog | None = None

    @property
    def log(self) -> PortLog | None:
        return self._log

    def set(self, log: PortLog | None) -> None:
        self._log = log

    def detach(self) -> PortLog | None:
        log, self._log = self._log, None
        return log

    def file_paths(self) -> tuple[Path, ...]:
        """The active log's files, or an empty tuple with no log."""
        log = self._log
        return tuple(log.files.values()) if log is not None else ()

    def rx(self, data: bytes) -> None:
        log = self._log
        if log is not None:
            log.rx(data)

    def tx(self, segment: bytes, source: str, accepted: int | None = None) -> None:
        """Record the bytes of *segment* the port accepted (all of it when *accepted* is None)."""
        log = self._log
        if log is None:
            return
        sent = segment if accepted is None else segment[:accepted]
        if sent:
            log.tx(sent, source)

    def event(self, name: str, **fields: Any) -> None:
        log = self._log
        if log is not None:
            log.event(name, **fields)

    def close_log(self) -> None:
        """Record a ``close`` event, then flush and close the log if one is active."""
        log = self.detach()
        if log is not None:
            log.event(CLOSE_EVENT)
            log.close()


# ---------------------------------------------------------------------------
# Choosing and creating log files
# ---------------------------------------------------------------------------


def open_log_files(
    directory: Path, formats: tuple[str, ...], now: datetime
) -> dict[str, tuple[Path, BinaryIO]]:
    """Open one new file per format under *directory*, all sharing one base name.

    Takes the next running number until no file of any chosen format has the name. If
    another connection takes a name in the meantime, removes the files opened so far
    and retries with the next number.

    Raises:
        OSError: The directory cannot be created, or every name up to the limit is taken.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    for number in range(MAX_NAME_COLLISIONS):
        base = log_base_name(LOG_FILE_PREFIX, now, number)
        paths = {name: directory / f"{base}.{FORMATS[name].extension}" for name in formats}
        if any(path.is_symlink() or path.exists() for path in paths.values()):
            continue
        opened: dict[str, tuple[Path, BinaryIO]] = {}
        try:
            for name, path in paths.items():
                opened[name] = (path, open_exclusive(path))
        except FileExistsError:
            for path, fh in opened.values():
                fh.close()
                path.unlink(missing_ok=True)
            continue
        return opened
    raise OSError(f"No free log file name in {directory} after {MAX_NAME_COLLISIONS} tries.")
