"""Input and output tools: read, readline, read_until, write, and flush, with write pacing parsing."""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.responses import (
    _coerce_bool,
    _err,
    _ok,
    script_running_error,
)
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.writes import duration_error, run_write_job
from serial_mcp_server.port.pacing import (
    MAX_PACING_DELAY_MS,
    NO_PACING,
    Pacing,
    paced_transfer_seconds,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum read size to prevent accidental huge allocations.
MAX_READ_BYTES = 1_048_576  # 1 MiB
MAX_WRITE_DELAY_MS = MAX_PACING_DELAY_MS
PACING_KEYS = ("inter_char_delay_ms", "chunk_size", "chunk_delay_ms")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_data(raw: bytes, fmt: str, encoding: str) -> dict[str, Any]:
    """Format raw bytes according to *fmt* ("text", "hex", or "base64")."""
    if fmt == "hex":
        return {"data": raw.hex(), "format": "hex"}
    if fmt == "base64":
        return {"data": base64.b64encode(raw).decode("ascii"), "format": "base64"}
    # default: text
    return {"data": raw.decode(encoding, errors="replace"), "format": "text", "encoding": encoding}


def _parse_pacing_number(value: Any, name: str, maximum: int | None = None) -> int:
    """Parse a non-negative integer used for write pacing."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be a non-negative integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer.") from exc
    if number < 0 or (maximum is not None and number > maximum):
        bound = f" from 0 to {maximum}" if maximum is not None else " greater than or equal to 0"
        raise ValueError(f"{name} must be an integer{bound}.")
    return number


def parse_write_pacing(args: dict[str, Any], default: Pacing = NO_PACING) -> Pacing:
    """Return the pacing for one write, or *default* when *args* set none."""
    if not any(key in args for key in PACING_KEYS):
        return default

    inter_char_delay_ms = _parse_pacing_number(
        args.get("inter_char_delay_ms", 0), "inter_char_delay_ms", MAX_WRITE_DELAY_MS
    )
    chunk_size = _parse_pacing_number(args.get("chunk_size", 0), "chunk_size")
    chunk_delay_ms = _parse_pacing_number(args.get("chunk_delay_ms", 0), "chunk_delay_ms", MAX_WRITE_DELAY_MS)
    if (chunk_size == 0) != (chunk_delay_ms == 0):
        raise ValueError("chunk_size and chunk_delay_ms must both be positive to enable chunk pacing.")
    if inter_char_delay_ms and chunk_size:
        raise ValueError("inter_char_delay_ms cannot be combined with chunk pacing.")
    return Pacing(inter_char_delay_ms, chunk_size, chunk_delay_ms)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    # ---- read ----
    # Reads consume bytes from the shared buffer, so they are not read-only.
    Tool(
        name="serial.read",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Read up to nbytes from a serial port. Returns immediately with whatever data "
            "is available within the timeout. Use serial.readline or serial.read_until for "
            "line-oriented reads."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "nbytes": {
                    "type": ["integer", "string"],
                    "default": 256,
                    "description": "Maximum bytes to read (default 256).",
                },
                "timeout_ms": {
                    "type": ["integer", "string"],
                    "description": "Override read timeout for this call only (milliseconds).",
                },
                "as": {
                    "type": "string",
                    "enum": ["text", "hex", "base64"],
                    "default": "text",
                    "description": "Output format: text (decoded string), hex, or base64. Default text.",
                },
            },
            "required": ["connection_id"],
        },
    ),
    # ---- write ----
    Tool(
        name="serial.write",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Write data to a serial port. Returns bytes_written, the bytes the host serial layer accepted. "
            "Pace a slow receiver with inter_char_delay_ms, or chunk_size with chunk_delay_ms. "
            "A paced write whose estimated time exceeds the server cap (SERIAL_MCP_MAX_WRITE_SECONDS, "
            "default 60 s) is rejected before anything is sent; split the data across calls. "
            "A write that stops early returns write_aborted with bytes_written."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "data": {"type": "string", "description": "Data to write."},
                "encoding": {
                    "type": "string",
                    "description": "Override encoding for this call (defaults to connection encoding).",
                },
                "append_newline": {
                    "type": ["boolean", "string"],
                    "default": False,
                    "description": "Append the connection's newline character after data (default false).",
                },
                "newline": {
                    "type": "string",
                    "description": "Override newline for append_newline (defaults to connection newline).",
                },
                "as": {
                    "type": "string",
                    "enum": ["text", "hex", "base64"],
                    "default": "text",
                    "description": "How to interpret 'data': text (encode with encoding), hex, or base64.",
                },
                "inter_char_delay_ms": {
                    "type": ["integer", "string"],
                    "description": (
                        "Override the connection's delay between transmitted characters in milliseconds "
                        "for this write (0 disables pacing; max 1000). Cannot be combined with chunk pacing."
                    ),
                },
                "chunk_size": {
                    "type": ["integer", "string"],
                    "description": "Override bytes per chunk for this write; set with chunk_delay_ms.",
                },
                "chunk_delay_ms": {
                    "type": ["integer", "string"],
                    "description": "Override delay after each drained chunk in milliseconds (max 1000).",
                },
            },
            "required": ["connection_id", "data"],
        },
    ),
    # ---- readline ----
    Tool(
        name="serial.readline",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Read a line from the serial port (reads until the newline character is received "
            "or max_bytes is reached). Uses the connection's newline setting by default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "timeout_ms": {
                    "type": ["integer", "string"],
                    "description": "Override read timeout (milliseconds).",
                },
                "max_bytes": {
                    "type": ["integer", "string"],
                    "default": 4096,
                    "description": "Maximum bytes to read (default 4096).",
                },
                "newline": {
                    "type": "string",
                    "description": "Override line terminator (defaults to connection newline).",
                },
                "as": {
                    "type": "string",
                    "enum": ["text", "hex", "base64"],
                    "default": "text",
                    "description": "Output format (default text).",
                },
            },
            "required": ["connection_id"],
        },
    ),
    # ---- read_until ----
    Tool(
        name="serial.read_until",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Read from the serial port until a delimiter string is received or max_bytes is reached."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "delimiter": {
                    "type": "string",
                    "default": "\\n",
                    "description": "Delimiter to read until (default \\n).",
                },
                "timeout_ms": {
                    "type": ["integer", "string"],
                    "description": "Override read timeout (milliseconds).",
                },
                "max_bytes": {
                    "type": ["integer", "string"],
                    "default": 4096,
                    "description": "Maximum bytes to read (default 4096).",
                },
                "as": {
                    "type": "string",
                    "enum": ["text", "hex", "base64"],
                    "default": "text",
                    "description": "Output format (default text).",
                },
            },
            "required": ["connection_id"],
        },
    ),
    # ---- flush ----
    Tool(
        name="serial.flush",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Discard pending data in the input buffer, the output buffer, or both. "
            "Flushing output also cancels writes still queued, which return write_aborted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "what": {
                    "type": "string",
                    "enum": ["input", "output", "both"],
                    "default": "both",
                    "description": "Which buffer to flush: input, output, or both (default both).",
                },
            },
            "required": ["connection_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_read(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    nbytes = min(int(args.get("nbytes", 256)), MAX_READ_BYTES)
    fmt = args.get("as", "text")
    timeout_ms = args.get("timeout_ms")
    timeout_s = int(timeout_ms) / 1000.0 if timeout_ms is not None else conn.timeout

    raw = await asyncio.to_thread(conn.buffer.read, nbytes, timeout_s)

    conn.last_seen_ts = time.time()
    formatted = _format_data(raw, fmt, conn.encoding)
    return _ok(
        message=f"Read {len(raw)} byte(s) from {conn.port}.",
        n_read=len(raw),
        **formatted,
    )


async def handle_write(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    data_str = args["data"]
    fmt = args.get("as", "text")
    encoding = args.get("encoding", conn.encoding)
    append_newline = _coerce_bool(args.get("append_newline", False))
    newline = args.get("newline", conn.newline)
    try:
        pacing = parse_write_pacing(args, conn.writer.pacing)
    except ValueError as exc:
        return _err("invalid_params", str(exc))

    # Convert to bytes based on format
    if fmt == "hex":
        try:
            payload = bytes.fromhex(data_str)
        except ValueError:
            return _err("invalid_value", "data is not valid hex.")
    elif fmt == "base64":
        try:
            payload = base64.b64decode(data_str)
        except Exception:
            return _err("invalid_value", "data is not valid base64.")
    else:
        payload = data_str.encode(encoding, errors="replace")

    if append_newline:
        payload += newline.encode(encoding, errors="replace")

    too_long = duration_error(
        paced_transfer_seconds(pacing, len(payload), conn.baudrate), state.settings.max_write_seconds
    )
    if too_long is not None:
        return too_long

    # The writer thread serializes this with mirror input, so payloads never interleave.
    n_written, error = await run_write_job(conn, payload, pacing)
    if error is not None:
        return error
    conn.last_seen_ts = time.time()
    return _ok(
        message=f"Wrote {n_written} byte(s) to {conn.port}.",
        bytes_written=n_written,
    )


async def handle_readline(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    max_bytes = min(int(args.get("max_bytes", 4096)), MAX_READ_BYTES)
    fmt = args.get("as", "text")
    timeout_ms = args.get("timeout_ms")
    newline = args.get("newline", conn.newline)
    expected = newline.encode(conn.encoding, errors="replace")
    timeout_s = int(timeout_ms) / 1000.0 if timeout_ms is not None else conn.timeout

    raw = await asyncio.to_thread(conn.buffer.read_until, expected, max_bytes, timeout_s)

    conn.last_seen_ts = time.time()
    formatted = _format_data(raw, fmt, conn.encoding)
    return _ok(
        message=f"Read {len(raw)} byte(s) from {conn.port}.",
        n_read=len(raw),
        **formatted,
    )


async def handle_read_until(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    delimiter = args.get("delimiter", "\n")
    max_bytes = min(int(args.get("max_bytes", 4096)), MAX_READ_BYTES)
    fmt = args.get("as", "text")
    timeout_ms = args.get("timeout_ms")
    expected = delimiter.encode(conn.encoding, errors="replace")
    timeout_s = int(timeout_ms) / 1000.0 if timeout_ms is not None else conn.timeout

    raw = await asyncio.to_thread(conn.buffer.read_until, expected, max_bytes, timeout_s)

    conn.last_seen_ts = time.time()
    formatted = _format_data(raw, fmt, conn.encoding)
    return _ok(
        message=f"Read {len(raw)} byte(s) from {conn.port}.",
        n_read=len(raw),
        **formatted,
    )


async def handle_flush(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    what = args.get("what", "both")

    if what in ("input", "both"):
        await asyncio.to_thread(conn.ser.reset_input_buffer)
        # A script may have claimed the connection meanwhile; nothing awaits between this check and the discard.
        blocked = script_running_error(state, conn.connection_id)
        if blocked is not None:
            return blocked
        conn.buffer.clear()
    if what in ("output", "both"):
        # Queued and running writes would otherwise refill the buffer being flushed.
        conn.writer.discard_pending()
        await asyncio.to_thread(conn.ser.reset_output_buffer)

    conn.log_slot.event("flush", what=what)

    conn.last_seen_ts = time.time()
    return _ok(message=f"Flushed {what} buffer(s) on {conn.port}.")


# ---------------------------------------------------------------------------
# Handler map
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Any] = {
    "serial.read": handle_read,
    "serial.write": handle_write,
    "serial.readline": handle_readline,
    "serial.read_until": handle_read_until,
    "serial.flush": handle_flush,
}
