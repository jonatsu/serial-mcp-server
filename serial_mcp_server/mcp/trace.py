"""JSONL tracing for Serial MCP tool calls.

In-memory ring buffer with optional file sink. Tracing is on by default;
set ``SERIAL_MCP_TRACE=0`` to disable.  No serial imports.
"""

from __future__ import annotations

import collections
import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serial_mcp_server.config import Settings

# Default cut length of a traced payload (SERIAL_MCP_TRACE_MAX_BYTES sets the operator's value). The unit is
# characters of the value, not encoded bytes; see ``_truncated``.
DEFAULT_MAX_PAYLOAD_BYTES = 16384

# ---------------------------------------------------------------------------
# Sanitize args
# ---------------------------------------------------------------------------

# Device input and credentials: write data, and the source and variables of a script run.
_PAYLOAD_KEYS = {"data", "source", "vars"}


def sanitize_args(
    args: dict[str, Any], *, payloads: bool = False, max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
) -> dict[str, Any]:
    """Return a copy of *args* with payload values stripped, or cut to *max_bytes* when *payloads* is set.

    Only the top-level ``data``, ``source``, and ``vars`` arguments count as payloads. Every other argument,
    such as a log mark label or a search query, is kept whole.
    """
    out = copy.deepcopy(args)
    for key in _PAYLOAD_KEYS:
        if key not in out:
            continue
        if not payloads:
            del out[key]
        else:
            out[key] = _truncated(out[key], max_bytes)
    return out


def _truncated(value: Any, max_bytes: int) -> Any:
    """A string, or the JSON form of any other value, cut to *max_bytes*; short values stay as they are.

    *max_bytes* counts characters, not encoded bytes. A non-string value that is cut becomes its truncated JSON
    text, not a mapping.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > max_bytes:
        return text[:max_bytes] + "\u2026"
    return value


# ---------------------------------------------------------------------------
# TraceBuffer
# ---------------------------------------------------------------------------


class TraceBuffer:
    """Ring buffer of trace events with optional JSONL file sink.

    The ring keeps the newest *max_items* events, 2000 by default and not configurable through ``Settings``;
    older events leave the ring, so ``tail`` and ``status`` see only those. The file sink keeps every event:
    it is append-only, has no size cap or rotation, and is created with mode 0o644 before the umask applies.

    Arguments named ``data``, ``source``, or ``vars`` are payloads. ``sanitize`` removes them unless
    *payloads* is set. When they are traced, a payload longer than *max_payload_bytes* characters (16384 by
    default) is cut, so a long write or script shows only its start.
    """

    def __init__(
        self,
        max_items: int = 2000,
        file_path: str | None = None,
        *,
        payloads: bool = False,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        self._payloads = payloads
        self._max_payload_bytes = max_payload_bytes
        self._deque: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_items)
        self._file_path = file_path
        self._fh = None
        if file_path:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # Open atomically without following symlinks (TOCTOU-safe)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow:
                fd = os.open(file_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow, 0o644)
                self._fh = os.fdopen(fd, "a", encoding="utf-8")
            else:
                # O_NOFOLLOW unavailable (Windows) — fall back to check-then-open
                if p.is_symlink():
                    raise ValueError(f"Trace path is a symlink (refusing to follow): {file_path}")
                self._fh = open(file_path, "a", encoding="utf-8")  # noqa: SIM115

    def emit(self, event: dict[str, Any]) -> None:
        event["ts"] = datetime.now(UTC).isoformat()
        self._deque.append(event)
        if self._fh:
            self._fh.write(json.dumps(event, default=str) + "\n")
            self._fh.flush()

    def sanitize(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return *args* as this buffer records them: payloads stripped or cut, per its settings."""
        return sanitize_args(args, payloads=self._payloads, max_bytes=self._max_payload_bytes)

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        items = list(self._deque)
        return items[-n:]

    def status(self) -> dict[str, Any]:
        """Report the settings. ``event_count`` counts events still in the ring, up to *max_items*."""
        return {
            "enabled": True,
            "event_count": len(self._deque),
            "file_path": self._file_path,
            "payloads_logged": self._payloads,
            "max_payload_bytes": self._max_payload_bytes,
        }

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __del__(self) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_buffer: TraceBuffer | None = None


def get_trace_buffer() -> TraceBuffer | None:
    return _buffer


def init_trace(settings: Settings) -> TraceBuffer | None:
    """Called once at startup. Replaces the buffer: a new one if tracing is enabled, else ``None``."""
    global _buffer
    if _buffer is not None:
        _buffer.close()
    _buffer = None
    if settings.trace_enabled:
        _buffer = TraceBuffer(
            file_path=str(settings.data_root / "traces" / "trace.jsonl"),
            payloads=settings.trace_payloads,
            max_payload_bytes=settings.trace_max_bytes,
        )
    return _buffer
