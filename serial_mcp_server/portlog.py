"""Port log: a per-connection recording of the bytes on the wire.

This is separate from the tool-call trace in ``trace.py`` and from the Python
``logging`` output. A ``PortLog`` owns a queue and one disk thread, so file
writes never block the reader or writer threads. When the disk falls behind,
entries are dropped and a marker records how many bytes were lost.

No MCP imports; the script engine reuses the file-opening helpers.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from serial_mcp_server.specs import resolve_spec_root

logger = logging.getLogger("serial_mcp_server")

ENV_LOG = "SERIAL_MCP_LOG"
ENV_FORMATS = "SERIAL_MCP_LOG_FORMATS"
ENV_DIR = "SERIAL_MCP_LOG_DIR"
_TRUE_VALUES = ("1", "true", "yes")

DEFAULT_FORMATS = ("text",)
QUEUE_LIMIT_BYTES = 8 * 1024 * 1024
CLOSE_JOIN_TIMEOUT_S = 5.0
LOG_FILE_PREFIX = "serial-mcp-log"
MAX_NAME_COLLISIONS = 1000
FILE_MODE = 0o600
DIR_MODE = 0o700

KIND_RX = "rx"
KIND_TX = "tx"
KIND_EVENT = "event"
KIND_OVERFLOW = "overflow"


# ---------------------------------------------------------------------------
# Entries and format writers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One recorded item, stamped when it was queued."""

    kind: str
    wall: datetime
    offset: float  # seconds since the log started, monotonic
    data: bytes = b""
    source: str = ""
    name: str = ""
    fields: dict[str, Any] = field(default_factory=dict)


class FormatWriter(Protocol):
    """Writes entries to one open file. Called only from the disk thread."""

    def write(self, entry: Entry) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class _FileWriter:
    """Shared file plumbing for format writers; subclasses implement ``write``."""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class TextWriter(_FileWriter):
    """Received bytes, unchanged, so lines split where the device sent newlines."""

    def __init__(self, fh: BinaryIO, encoding: str) -> None:
        super().__init__(fh)  # received bytes are written as they came, so the encoding is unused
        self._at_line_start = True

    def write(self, entry: Entry) -> None:
        if entry.kind == KIND_RX and entry.data:
            self._fh.write(entry.data)
            self._at_line_start = entry.data.endswith(b"\n")
        elif entry.kind == KIND_OVERFLOW:
            lead = b"" if self._at_line_start else b"\n"
            count = entry.fields["dropped_bytes"]
            self._fh.write(lead + f"--- serial-mcp: {count} bytes not logged ---\n".encode("ascii"))
            self._at_line_start = True


class JsonlWriter(_FileWriter):
    """One JSON object per chunk in either direction, plus control events."""

    def __init__(self, fh: BinaryIO, encoding: str) -> None:
        super().__init__(fh)
        self._encoding = encoding if is_text_encoding(encoding) else "utf-8"

    def write(self, entry: Entry) -> None:
        record: dict[str, Any] = {"ts": _iso(entry.wall), "t": round(entry.offset, 6)}
        if entry.kind in (KIND_RX, KIND_TX):
            record["dir"] = entry.kind
            if entry.kind == KIND_TX:
                record["source"] = entry.source
            record["len"] = len(entry.data)
            record["hex"] = entry.data.hex()
            record["text"] = entry.data.decode(self._encoding, errors="replace")
        else:
            self._follow_configure(entry)
            record["event"] = "log_overflow" if entry.kind == KIND_OVERFLOW else entry.name
            record.update(entry.fields)
        self._fh.write(json.dumps(record, default=str).encode("utf-8") + b"\n")

    def _follow_configure(self, entry: Entry) -> None:
        """Decode later entries with the encoding a ``configure`` event switched to."""
        encoding = entry.fields.get("encoding") if entry.name == "configure" else None
        if encoding is not None and is_text_encoding(encoding):
            self._encoding = encoding


def is_text_encoding(name: object) -> bool:
    """Return True when *name* is a codec that ``str.encode`` accepts, which excludes bytes-to-bytes codecs like hex."""
    if not isinstance(name, str):
        return False
    try:
        "".encode(name)
        b"".decode(name)
    except (LookupError, UnicodeError):
        return False
    return True


@dataclass(frozen=True)
class LogFormat:
    extension: str
    make_writer: Callable[[BinaryIO, str], FormatWriter]


FORMATS: dict[str, LogFormat] = {
    "text": LogFormat("txt", TextWriter),
    "jsonl": LogFormat("jsonl", JsonlWriter),
}


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


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
        writers = {name: FORMATS[name].make_writer(fh, encoding) for name, (_, fh) in opened.items()}
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
                self._queue.append(self._overflow_entry(self._wall_clock(), self._monotonic() - self._start))
            self._closed = True
            self._cond.notify_all()
        self._thread.join(timeout=CLOSE_JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            # The disk thread closes the files itself when it finishes, so nothing leaks.
            logger.warning("Port log disk thread did not finish within %.0f s.", CLOSE_JOIN_TIMEOUT_S)

    def _put(
        self,
        kind: str,
        *,
        data: bytes = b"",
        source: str = "",
        name: str = "",
        fields: dict[str, Any] | None = None,
    ) -> None:
        wall, offset = self._wall_clock(), self._monotonic() - self._start
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
                # Idle: make what has been written visible to readers of the files.
                self._for_each_writer(lambda writer: writer.flush())
                with self._cond:
                    while not self._queue and not self._closed:
                        self._cond.wait()
                continue
            self._for_each_writer(lambda writer, item=entry: writer.write(item))
            if entry.kind != KIND_OVERFLOW:
                with self._cond:
                    self._entries += 1
        self._for_each_writer(lambda writer: writer.flush())
        self._for_each_writer(lambda writer: writer.close(), drop_on_error=False)

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
            log.event("close")
            log.close()


# ---------------------------------------------------------------------------
# Choosing and creating log files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogRequest:
    formats: tuple[str, ...]
    directory: Path


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


def resolve_log_request(log: bool | None, formats: Any = None) -> LogRequest | None:
    """Decide whether a connection logs, and in which formats and directory.

    An argument beats the environment: ``log=False`` disables logging, ``log=True`` or
    *formats* alone enables it, and otherwise ``SERIAL_MCP_LOG`` decides. Read at call
    time, so a change to the environment applies to the next connection.

    Raises:
        ValueError: A format list is invalid.
    """
    if log is None:
        log = formats is not None or os.environ.get(ENV_LOG, "").strip().lower() in _TRUE_VALUES
    if not log:
        return None
    if formats is not None:
        names = parse_formats(formats, "log_formats")
    else:
        names = parse_formats(os.environ.get(ENV_FORMATS, "").strip() or list(DEFAULT_FORMATS), ENV_FORMATS)
    env_dir = os.environ.get(ENV_DIR, "").strip()
    directory = Path(env_dir) if env_dir else resolve_spec_root() / "logs"
    return LogRequest(names, directory)


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
