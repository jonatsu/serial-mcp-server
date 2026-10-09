"""Port log: a per-connection recording of the bytes on the wire.

This is separate from the tool-call trace in ``trace.py`` and from the Python
``logging`` output. A ``PortLog`` owns a queue and one disk thread, so file
writes never block the reader or writer threads. When the disk falls behind,
entries are dropped and a marker records how many bytes were lost.

No MCP imports; the script engine reuses the file-opening helpers.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from serial_mcp_server.portlog_formats import (
    CLOSE_EVENT,
    FORMATS,
    KIND_EVENT,
    KIND_OVERFLOW,
    KIND_RX,
    KIND_TX,
    TEXT_TIMESTAMP_MODES,
    Entry,
    FormatWriter,
    TextTimestamps,
    WriterOptions,
)

if TYPE_CHECKING:
    from serial_mcp_server.config import Settings

logger = logging.getLogger("serial_mcp_server")

DEFAULT_TEXT_TIMESTAMPS = TextTimestamps.OFF
QUEUE_LIMIT_BYTES = 8 * 1024 * 1024
CLOSE_JOIN_TIMEOUT_S = 5.0
LOG_FILE_PREFIX = "serial-mcp-log"
MAX_NAME_COLLISIONS = 1000
FILE_MODE = 0o600
DIR_MODE = 0o700

# ---------------------------------------------------------------------------
# PortLog
# ---------------------------------------------------------------------------


class PortLog:
    """Queue plus disk thread for one connection's log files.

    ``rx``, ``tx``, and ``event`` never block: when the queued payload would
    exceed *queue_limit*, the entry is dropped and counted, and the next
    accepted entry is preceded by an overflow marker with the count. After
    ``close`` all three are no-ops.
    """

    def __init__(
        self,
        writers: dict[str, FormatWriter],
        files: dict[str, Path],
        *,
        queue_limit: int = QUEUE_LIMIT_BYTES,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._writers = dict(writers)
        self.files = dict(files)
        self._queue_limit = queue_limit
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._start = monotonic()
        self._cond = threading.Condition()
        self._queue: deque[Entry] = deque()
        self._queued_bytes = 0
        self._unreported_drops = 0
        self._dropped_total = 0
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

    def event(self, name: str, **fields: Any) -> None:
        self._put(KIND_EVENT, name=name, fields=fields)

    def status(self) -> dict[str, Any]:
        with self._cond:
            return {
                "formats": list(self.files),
                "files": {name: str(path) for name, path in self.files.items()},
                "entries": self._entries,
                "dropped_bytes": self._dropped_total,
            }

    def close(self) -> None:
        """Write what is queued, close the files, and stop accepting entries."""
        with self._cond:
            if self._closed:
                return
            if self._unreported_drops:
                self._queue.append(self._overflow_entry(self._wall_clock(), self._offset()))
            self._closed = True
            self._cond.notify_all()
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
    ) -> None:
        wall, offset = self._wall_clock(), self._offset()
        entry = Entry(kind, wall, offset, data, source, name, fields or {})
        with self._cond:
            if self._closed:
                return
            if len(data) and self._queued_bytes + len(data) > self._queue_limit:
                self._unreported_drops += len(data)
                self._dropped_total += len(data)
                return
            if self._unreported_drops:
                self._queue.append(self._overflow_entry(wall, offset))
            self._queue.append(entry)
            self._queued_bytes += len(data)
            self._cond.notify()

    def _overflow_entry(self, wall: datetime, offset: float) -> Entry:
        """Build the marker for dropped bytes and reset the count. Caller holds the lock."""
        marker = Entry(KIND_OVERFLOW, wall, offset, fields={"dropped_bytes": self._unreported_drops})
        self._unreported_drops = 0
        return marker

    def _run(self) -> None:
        while True:
            with self._cond:
                entry = self._queue.popleft() if self._queue else None
                if entry is not None:
                    self._queued_bytes -= len(entry.data)
                elif self._closed:
                    break
            if entry is None:
                self._idle()
                continue
            self._for_each_writer(lambda writer, item=entry: writer.write(item))
            self._for_each_writer(lambda writer, offset=entry.offset: writer.tick(offset))
            if entry.kind != KIND_OVERFLOW:
                with self._cond:
                    self._entries += 1
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


@dataclass(frozen=True)
class LogRequest:
    formats: tuple[str, ...]
    directory: Path
    text_timestamps: TextTimestamps = DEFAULT_TEXT_TIMESTAMPS


def parse_formats(value: Any, origin: str) -> tuple[str, ...]:
    """Validate a list (or comma-separated string) of format names.

    Raises:
        ValueError: *value* is empty, not a list of strings, or names an unknown format.
    """
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not all(isinstance(item, str) for item in items):
        raise ValueError(f"{origin} must be a list of format names.")
    names = tuple(dict.fromkeys(item.strip().lower() for item in items if item.strip()))
    if not names:
        raise ValueError(f"{origin} must name at least one format.")
    unknown = [name for name in names if name not in FORMATS]
    if unknown:
        raise ValueError(f"Unknown log format {unknown[0]!r} in {origin}. Choose from: {', '.join(FORMATS)}.")
    return names


def _parse_text_timestamps(value: Any, settings: Settings) -> TextTimestamps:
    """Validate the timestamp mode from an argument, else take the configured one.

    Raises:
        ValueError: The mode is not one of ``TEXT_TIMESTAMP_MODES``.
    """
    if value is None:
        return TextTimestamps(settings.log_text_timestamps)
    return _text_timestamps(value, "log_text_timestamps")


def _text_timestamps(value: Any, origin: str) -> TextTimestamps:
    mode = value.strip().lower() if isinstance(value, str) else value
    if mode not in TEXT_TIMESTAMP_MODES:
        raise ValueError(f"{origin} must be one of: {', '.join(TEXT_TIMESTAMP_MODES)}.")
    return TextTimestamps(mode)


def resolve_log_request(
    settings: Settings, log: bool | None, formats: Any = None, text_timestamps: Any = None
) -> LogRequest | None:
    """Decide whether a connection logs, and in which formats and directory.

    An argument beats the configuration: ``log=False`` disables logging, ``log=True`` or
    *formats* alone enables it, and otherwise ``settings.log_enabled`` (``SERIAL_MCP_LOG``) decides.

    Raises:
        ValueError: A format list or the timestamp mode is invalid.
    """
    if log is None:
        log = formats is not None or settings.log_enabled
    if not log:
        return None
    return build_log_request(settings, formats, text_timestamps)


def build_log_request(
    settings: Settings, formats: Any = None, text_timestamps: Any = None, formats_origin: str = "log_formats"
) -> LogRequest:
    """Build a request for a log that is definitely wanted, filling gaps from the configuration.

    *formats_origin* names where *formats* came from, so an error points at the caller's own option.

    Raises:
        ValueError: A format list or the timestamp mode is invalid.
    """
    names = parse_formats(formats, formats_origin) if formats is not None else settings.log_formats
    return LogRequest(names, settings.log_dir, _parse_text_timestamps(text_timestamps, settings))


def log_base_name(prefix: str, now: datetime, number: int) -> str:
    """``<prefix>-<DDMMYYYY>-<HHMM>`` with ``-<number>`` appended after the first."""
    base = f"{prefix}-{now:%d%m%Y-%H%M}"
    return base if number == 0 else f"{base}-{number}"


def open_exclusive(path: Path) -> BinaryIO:
    """Create *path* for writing; never follow a symlink or reuse an existing file.

    Raises:
        FileExistsError: Something already has this name, including a symlink.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, FILE_MODE), "wb")


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
