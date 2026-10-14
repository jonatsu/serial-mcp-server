"""JSONL tracing for Serial MCP tool calls.

In-memory ring buffer with optional file sink. Tracing is on by default;
set ``SERIAL_MCP_TRACE=0`` to disable.  No serial imports.
"""

from __future__ import annotations

import collections
import copy
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from serial_mcp_server.config import DEFAULT_TRACE_MAX_BYTES
from serial_mcp_server.private_files import FILE_MODE

if TYPE_CHECKING:
    from serial_mcp_server.config import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sanitize args
# ---------------------------------------------------------------------------

# Device input and credentials: write data, and the source and variables of a script run.
_PAYLOAD_KEYS = {"data", "source", "vars"}


def sanitize_args(
    args: dict[str, Any], *, payloads: bool = False, max_bytes: int = DEFAULT_TRACE_MAX_BYTES
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
    older events leave the ring, so ``tail`` and ``status`` see only those. The file sink keeps every event until it fails; memory tracing continues after failure.
    It is append-only, has no size cap or rotation, and is readable by its owner only (``FILE_MODE``), like the
    port logs, because traced payloads are the data sent to the device.

    Arguments named ``data``, ``source``, or ``vars`` are payloads. ``sanitize`` removes them unless
    *payloads* is set. When they are traced, a payload longer than *max_payload_bytes* characters
    (``DEFAULT_TRACE_MAX_BYTES`` by default) is cut, so a long write or script shows only its start.
    """

    def __init__(
        self,
        max_items: int = 2000,
        file_path: str | None = None,
        *,
        payloads: bool = False,
        max_payload_bytes: int = DEFAULT_TRACE_MAX_BYTES,
    ) -> None:
        self._payloads = payloads
        self._max_payload_bytes = max_payload_bytes
        self._deque: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_items)
        self._file_path = file_path
        self._fh = None
        self._file_state: Literal["disabled", "active", "failed", "closed"] = "disabled"
        self._file_error: dict[str, str | int | None] | None = None
        if file_path:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # Open atomically without following symlinks (TOCTOU-safe)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow:
                fd = os.open(file_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow, FILE_MODE)
                # The mode above applies only to a new file; a trace file from an earlier run is tightened too.
                os.fchmod(fd, FILE_MODE)
                self._fh = os.fdopen(fd, "a", encoding="utf-8")
            else:
                # O_NOFOLLOW unavailable (Windows) — fall back to check-then-open
                if p.is_symlink():
                    raise ValueError(f"Trace path is a symlink (refusing to follow): {file_path}")
                self._fh = open(file_path, "a", encoding="utf-8")  # noqa: SIM115

            self._file_state = "active"

    def emit(self, event: dict[str, Any]) -> None:
        event["ts"] = datetime.now(UTC).isoformat()
        self._deque.append(event)
        if self._fh:
            line = json.dumps(event, default=str) + "\n"
            try:
                self._fh.write(line)
                self._fh.flush()
            except (OSError, ValueError) as exc:
                self._record_file_failure(exc)
                self.close()

    def sanitize(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return *args* as this buffer records them: payloads stripped or cut, per its settings."""
        return sanitize_args(args, payloads=self._payloads, max_bytes=self._max_payload_bytes)

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        items = list(self._deque)
        return items[-n:]

    def status(self) -> dict[str, Any]:
        """Report settings and sink health; ``event_count`` counts events still in the ring."""
        return {
            "enabled": True,
            "event_count": len(self._deque),
            "file_path": self._file_path,
            "file_state": self._file_state,
            "file_error": dict(self._file_error) if self._file_error else None,
            "payloads_logged": self._payloads,
            "max_payload_bytes": self._max_payload_bytes,
        }

    def _record_file_failure(self, exc: OSError | ValueError) -> None:
        if self._file_state == "failed":
            return
        self._file_state = "failed"
        self._file_error = {
            "class": type(exc).__name__,
            "errno": exc.errno if isinstance(exc, OSError) else None,
        }
        # Exception messages can contain traced payloads; report only safe diagnostic fields.
        logger.warning(
            "Trace file sink failed; memory tracing continues (class=%s, errno=%s)",
            self._file_error["class"],
            self._file_error["errno"],
        )

    def close(self) -> None:
        # Detach before closing: a failed flush during close must never be retried by shutdown.
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.close()
        except (OSError, ValueError) as exc:
            self._record_file_failure(exc)
        else:
            if self._file_state == "active":
                self._file_state = "closed"

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
