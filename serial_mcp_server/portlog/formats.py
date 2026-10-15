"""Port log entries and the file formats that record them.

Each format is a ``FormatWriter`` over one open binary file. The ``FORMATS``
registry maps a format name to its file extension and writer factory, so a new
format needs no change to the log's queue or disk thread.
"""

from __future__ import annotations

import codecs
import json
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, BinaryIO, Protocol

KIND_RX = "rx"
KIND_TX = "tx"
KIND_EVENT = "event"
KIND_OVERFLOW = "overflow"

OPEN_EVENT = "open"
CLOSE_EVENT = "close"
MARK_EVENT = "mark"
BUFFER_DROP_EVENT = "buffer_drop"
CONFIGURE_EVENT = "configure"
LOG_ADMISSION_FAILED_EVENT = "log_admission_failed"
SEND_FILE_START_EVENT = "send_file_start"
SEND_FILE_END_EVENT = "send_file_end"


class TextTimestamps(StrEnum):
    """How the text log prefixes each line."""

    OFF = "off"
    ELAPSED = "elapsed"
    SYSTEM = "system"


TEXT_TIMESTAMP_MODES = tuple(mode.value for mode in TextTimestamps)
# Silence, in seconds, after which the lines log writes an unfinished line as a partial entry. A prompt that
# ends without a newline therefore reaches that file after this wait, a size flush, a marker, or log close.
PARTIAL_IDLE_S = 0.5
# Emit size-triggered partials at 64 KiB per direction, keeping sustained output bounded without tiny records.
# Decoder carry is bounded by the same limit; oversized encoded sequences are finalized with replacement.
LINE_BUFFER_BYTES = 64 * 1024
_EPOCH = datetime.fromtimestamp(0, UTC)


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


@dataclass(frozen=True)
class WriterOptions:
    """Settings every format factory receives; a format uses the ones it needs."""

    encoding: str = "utf-8"
    text_timestamps: TextTimestamps = TextTimestamps.OFF


class FormatWriter(Protocol):
    """Writes entries to one open file. Called only from the disk thread."""

    def write(self, entry: Entry) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def pending_deadline(self) -> float | None:
        """Log offset at which ``tick`` has work to do, or None when nothing waits."""
        ...

    def tick(self, offset: float) -> None:
        """Do time-driven work, such as flushing a partial line, at log offset *offset*."""
        ...


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


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class _FileWriter:
    """Shared file plumbing for format writers; subclasses implement ``write``."""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def pending_deadline(self) -> float | None:
        return None

    def tick(self, offset: float) -> None:
        return None


class _Decoder:
    """Decodes entry bytes with the connection's encoding, following ``serial.configure``."""

    def __init__(self, encoding: str) -> None:
        self._encoding = encoding if is_text_encoding(encoding) else "utf-8"

    def decode(self, data: bytes) -> str:
        return data.decode(self._encoding, errors="replace")

    def follow(self, entry: Entry) -> None:
        """Switch to the encoding a ``configure`` event names, if it is a usable one."""
        encoding = entry.fields.get("encoding") if entry.name == CONFIGURE_EVENT else None
        if encoding is not None and is_text_encoding(encoding):
            self._encoding = encoding


def _split_lines(data: bytes) -> Iterator[tuple[bytes, bool]]:
    """Yield ``(piece, ends_line)`` for each newline-separated piece of *data*.

    A chunk that ends on a newline has no trailing piece, so no empty phantom line appears.
    """
    parts = data.split(b"\n")
    for index, part in enumerate(parts):
        ends_line = index < len(parts) - 1
        if not ends_line and not part:
            return
        yield part, ends_line


def _stamp(wall: datetime, offset: float) -> dict[str, Any]:
    """The ``ts`` (ISO UTC) and ``t`` (seconds since the log started) fields every JSON record starts with."""
    return {"ts": _iso(wall), "t": round(offset, 6)}


def _event_record(entry: Entry) -> dict[str, Any]:
    record = _stamp(entry.wall, entry.offset)
    record["event"] = "log_overflow" if entry.kind == KIND_OVERFLOW else entry.name
    record.update(entry.fields)
    return record


def _write_json_line(fh: BinaryIO, record: dict[str, Any]) -> None:
    fh.write(json.dumps(record, default=str).encode("utf-8") + b"\n")


class TextWriter(_FileWriter):
    """Received bytes, unchanged, so lines split where the device sent newlines.

    With ``text_timestamps`` set to ``elapsed`` or ``system``, each line starts with a
    grabserial-style prefix ``[<time> <delta>] ``, stamped when the line's first byte
    arrived. Transmitted bytes are never written.
    """

    def __init__(self, fh: BinaryIO, options: WriterOptions) -> None:
        super().__init__(fh)
        self._mode = options.text_timestamps
        self._at_line_start = True
        self._first_offset: float | None = None
        self._previous_offset: float | None = None

    def write(self, entry: Entry) -> None:
        if entry.kind == KIND_RX and entry.data:
            self._write_received(entry)
        elif entry.kind == KIND_OVERFLOW:
            lead = b"" if self._at_line_start else b"\n"
            count = entry.fields["dropped_bytes"]
            events = entry.fields.get("dropped_events", 0)
            loss = f"{count} bytes not logged"
            if events:
                loss += f"; {events} events not logged"
            self._fh.write(lead + f"--- serial-mcp: {loss} ---\n".encode("ascii"))
            self._at_line_start = True
        elif entry.name == LOG_ADMISSION_FAILED_EVENT:
            lead = b"" if self._at_line_start else b"\n"
            self._fh.write(lead + f"--- serial-mcp: {entry.fields['reason']} ---\n".encode("ascii"))
            self._at_line_start = True

    def _write_received(self, entry: Entry) -> None:
        if self._mode == TextTimestamps.OFF:
            self._fh.write(entry.data)
            self._at_line_start = entry.data.endswith(b"\n")
            return
        out = bytearray()
        for part, ends_line in _split_lines(entry.data):
            if self._at_line_start:
                out += self._prefix(entry).encode("ascii")
            out += part
            if ends_line:
                out += b"\n"
            self._at_line_start = ends_line
        self._fh.write(bytes(out))

    def _prefix(self, entry: Entry) -> str:
        if self._first_offset is None:
            self._first_offset = entry.offset
        delta = 0.0 if self._previous_offset is None else entry.offset - self._previous_offset
        self._previous_offset = entry.offset
        if self._mode == TextTimestamps.ELAPSED:
            return f"[{entry.offset - self._first_offset:4.6f} {delta:2.6f}] "
        return f"[{entry.wall.astimezone(UTC):%H:%M:%S.%f} {delta:2.6f}] "


class RawWriter(_FileWriter):
    """Received bytes only, byte-exact, for replay and binary diffing.

    It records nothing else, not even an overflow marker, so bytes dropped under ``QUEUE_LIMIT_BYTES`` leave
    a gap that only the log's ``dropped_bytes`` count reveals.
    """

    def __init__(self, fh: BinaryIO, options: WriterOptions) -> None:
        super().__init__(fh)

    def write(self, entry: Entry) -> None:
        if entry.kind == KIND_RX:
            self._fh.write(entry.data)


class JsonlWriter(_FileWriter):
    """One JSON object per chunk in either direction, plus control events.

    A chunk is what the reader or writer handled at once, not a line. ``hex`` is exact; ``text`` is the same
    bytes decoded with replacement, so invalid or split multi-byte characters appear as U+FFFD.
    """

    def __init__(self, fh: BinaryIO, options: WriterOptions) -> None:
        super().__init__(fh)
        self._decoder = _Decoder(options.encoding)

    def write(self, entry: Entry) -> None:
        if entry.kind not in (KIND_RX, KIND_TX):
            self._decoder.follow(entry)
            _write_json_line(self._fh, _event_record(entry))
            return
        record = {**_stamp(entry.wall, entry.offset), "dir": entry.kind}
        if entry.kind == KIND_TX:
            record["source"] = entry.source
        record["len"] = len(entry.data)
        record["hex"] = entry.data.hex()
        record["text"] = self._decoder.decode(entry.data)
        _write_json_line(self._fh, record)


@dataclass
class _Line:
    """The bytes of one direction's line that has not ended yet."""

    data: bytearray = field(default_factory=bytearray)
    wall: datetime = _EPOCH
    offset: float = 0.0  # when the first pending byte arrived
    last_offset: float = 0.0  # when the latest pending byte arrived
    continued: bool = False  # an earlier part of this line was already written as a partial
    # Each span accounts for at least one pending byte, so byte bounds also bound timestamp metadata.
    spans: deque[tuple[int, datetime, float]] = field(default_factory=deque)

    def retain_tail(self, size: int) -> None:
        """Keep the source times of bytes carried through a size flush, including decoder-owned bytes."""
        retained: deque[tuple[int, datetime, float]] = deque()
        while size:
            count, wall, offset = self.spans.pop()
            count = min(count, size)
            retained.appendleft((count, wall, offset))
            size -= count
        self.spans = retained
        if retained:
            _, self.wall, self.offset = retained[0]


class LinesWriter(_FileWriter):
    """One JSON object per line, per direction, stamped when the line's first byte arrived.

    A line with no newline after ``PARTIAL_IDLE_S`` of silence is written with
    ``"partial": true``, so a prompt such as ``=> `` still appears. Bytes that arrive
    later on the same line start an entry with ``"continued": true``, so joining a
    partial entry with its continuations restores the line.

    ``LINE_BUFFER_BYTES`` also triggers a partial, carrying decoder state into the next segment. Decoder carry
    has the same bound; a sequence that fills it is finalized with replacement. A trailing carriage return is
    held across size flushes so complete lines still omit CRLF. Other text is decoded with replacement.
    """

    def __init__(self, fh: BinaryIO, options: WriterOptions, idle_s: float = PARTIAL_IDLE_S) -> None:
        super().__init__(fh)
        self._encoding = options.encoding if is_text_encoding(options.encoding) else "utf-8"
        self._decoders = {
            kind: codecs.getincrementaldecoder(self._encoding)(errors="replace")
            for kind in (KIND_RX, KIND_TX)
        }
        self._idle_s = idle_s
        self._lines = {KIND_RX: _Line(), KIND_TX: _Line()}

    def write(self, entry: Entry) -> None:
        if entry.kind in self._lines:
            self._feed(entry)
        elif entry.kind == KIND_OVERFLOW or entry.name in (MARK_EVENT, LOG_ADMISSION_FAILED_EVENT):
            # Keep the marker where it happened, even if that interrupts a line.
            self._flush_partials()
            _write_json_line(self._fh, _event_record(entry))
        elif entry.name == CONFIGURE_EVENT and is_text_encoding(entry.fields.get("encoding")):
            self._flush_partials()
            self._encoding = entry.fields["encoding"]
            self._decoders = {
                kind: codecs.getincrementaldecoder(self._encoding)(errors="replace") for kind in self._lines
            }

    def pending_deadline(self) -> float | None:
        waiting = [
            line.last_offset + self._idle_s for kind, line in self._lines.items() if self._pending(kind)
        ]
        return min(waiting) if waiting else None

    def tick(self, offset: float) -> None:
        for kind, line in self._lines.items():
            if self._pending(kind) and offset - line.last_offset >= self._idle_s:
                self._emit(kind, line, partial=True)

    def close(self) -> None:
        try:
            self._flush_partials()
        finally:
            super().close()

    def _feed(self, entry: Entry) -> None:
        line = self._lines[entry.kind]
        # Judge silence by the entries' own times, so the result does not depend on how far the disk thread lags.
        self.tick(entry.offset)
        for part, ends_line in _split_lines(entry.data):
            remaining = memoryview(part)
            while remaining:
                if not self._pending(entry.kind):
                    line.wall, line.offset = entry.wall, entry.offset
                count = min(len(remaining), LINE_BUFFER_BYTES - len(line.data))
                line.data += remaining[:count]
                line.spans.append((count, entry.wall, entry.offset))
                remaining = remaining[count:]
                line.last_offset = entry.offset
                if len(line.data) == LINE_BUFFER_BYTES and (remaining or not ends_line):
                    self._emit(entry.kind, line, partial=True, size_flush=True)
            if ends_line:
                if not self._pending(entry.kind):
                    line.wall, line.offset = entry.wall, entry.offset
                self._emit(entry.kind, line, partial=False)

    def _pending(self, kind: str) -> bool:
        return bool(self._lines[kind].data or self._decoders[kind].getstate()[0])

    def _flush_partials(self) -> None:
        for kind, line in self._lines.items():
            if self._pending(kind):
                self._emit(kind, line, partial=True)

    def _emit(self, kind: str, line: _Line, *, partial: bool, size_flush: bool = False) -> None:
        raw = bytes(line.data) if partial else bytes(line.data).removesuffix(b"\r")
        carry_cr = size_flush and raw.endswith(b"\r")
        if carry_cr:
            raw = raw[:-1]
        decoder = self._decoders[kind]
        text = decoder.decode(raw, final=not size_flush)
        if size_flush and len(decoder.getstate()[0]) >= LINE_BUFFER_BYTES:
            text += decoder.decode(b"", final=True)
            decoder.reset()
        elif not size_flush:
            decoder.reset()
        record = {**_stamp(line.wall, line.offset), "dir": kind, "text": text}
        if partial:
            record["partial"] = True
        if line.continued:
            record["continued"] = True
        _write_json_line(self._fh, record)
        line.data.clear()
        if carry_cr:
            line.data += b"\r"
        line.retain_tail(len(decoder.getstate()[0]) + int(carry_cr))
        line.continued = partial


@dataclass(frozen=True)
class LogFormat:
    extension: str
    make_writer: Callable[[BinaryIO, WriterOptions], FormatWriter]


FORMATS: dict[str, LogFormat] = {
    "text": LogFormat("txt", TextWriter),
    "jsonl": LogFormat("jsonl", JsonlWriter),
    "lines": LogFormat("lines.jsonl", LinesWriter),
    "raw": LogFormat("bin", RawWriter),
}


# Tool schemas shared by serial.open and serial.log.start, so the format knowledge has one source.
LOG_FORMATS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "enum": list(FORMATS)},
    "description": (
        "Port log file formats; default text. "
        "text: received bytes unchanged. "
        "jsonl: every chunk in both directions plus control events. "
        "lines: one entry per line, with idle or 64 KiB segments flagged partial and later segments continued. "
        "raw: received bytes, byte-exact."
    ),
}
LOG_TEXT_TIMESTAMPS_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": list(TEXT_TIMESTAMP_MODES),
    "description": (
        "Prefix each line of the text log with [time delta]; default off. "
        "elapsed: seconds since the first received byte. system: UTC wall-clock time. "
        "delta: seconds since the previous line."
    ),
}
