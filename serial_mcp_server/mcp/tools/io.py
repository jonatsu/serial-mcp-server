"""Input and output tools: read, readline, read_until, write, and flush, with write pacing parsing."""

from __future__ import annotations

import base64
import time
from typing import Any

import anyio
import anyio.to_thread
from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.responses import (
    _coerce_bool,
    _err,
    _ok,
    script_running_error,
)
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.tools.schemas import (
    CONNECTION_ID,
    DATA_FORMAT,
    READ_TIMEOUT_MS,
    max_bytes,
    pacing_properties,
)
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

# Largest nbytes or max_bytes a read tool honors. A bigger request is clamped without an error, so check
# n_read in the reply. It equals the read buffer's capacity, so a read can never return more than is held.
MAX_READ_BYTES = 1_048_576  # 1 MiB
MAX_WRITE_DELAY_MS = MAX_PACING_DELAY_MS
PACING_KEYS = ("inter_char_delay_ms", "chunk_size", "chunk_delay_ms")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_data(raw: bytes, fmt: str, encoding: str) -> dict[str, Any]:
    """Format raw bytes according to *fmt* ("text", "hex", or "base64").

    Any other *fmt* means text. Text is decoded per call with replacement, so invalid bytes, and a multi-byte
    character split between two reads, become U+FFFD; hex and base64 are exact.
    """
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
            "Use to see what the device sent: after opening, after each write, or whenever output may have "
            "arrived. Returns as soon as any bytes are buffered, up to nbytes, or an empty result once the read "
            "timeout passes. Reading consumes the bytes. For a reply that ends in a known line ending or prompt, "
            "serial.readline and serial.read_until wait for the whole of it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "nbytes": max_bytes(256),
                "timeout_ms": READ_TIMEOUT_MS,
                "as": DATA_FORMAT,
            },
            "required": ["connection_id"],
        },
    ),
    # ---- write ----
    Tool(
        name="serial.write",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use to send a command or data to the device; read the reply afterwards. Set append_newline=true to "
            "end a command with the connection's line ending. Returns bytes_written, the bytes the host serial "
            "layer accepted. Pace a device that drops fast input with inter_char_delay_ms, or with chunk_size "
            "and chunk_delay_ms. A paced write whose estimated time exceeds the server cap "
            "(SERIAL_MCP_MAX_WRITE_SECONDS, default 60 s) is rejected before anything is sent; split the data "
            "across calls or lower the pacing. For a long prepared input, serial.send_file keeps it out of the "
            "arguments. A write that stops early returns "
            "write_aborted with bytes_written. While a script runs on the connection, writes return "
            "script_running."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "data": {
                    "type": "string",
                    "description": "The text or bytes to send, interpreted as 'as' says.",
                },
                "encoding": {
                    "type": "string",
                    "description": "Encoding for this call only; default the connection's encoding.",
                },
                "append_newline": {
                    "type": ["boolean", "string"],
                    "default": False,
                    "description": "Send the connection's line ending after data (default false).",
                },
                "newline": {
                    "type": "string",
                    "description": "Line ending that append_newline sends, for this call only.",
                },
                "as": {
                    "type": "string",
                    "enum": ["text", "hex", "base64"],
                    "default": "text",
                    "description": "How to read data: text (encoded with the encoding), hex, or base64.",
                },
                **pacing_properties(per_call=True),
            },
            "required": ["connection_id", "data"],
        },
    ),
    # ---- readline ----
    Tool(
        name="serial.readline",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use to read one line of a line-based reply: waits until the connection's line ending arrives, "
            "max_bytes is reached, or the read timeout passes, and returns what it has, including the line "
            "ending when it arrived."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "timeout_ms": READ_TIMEOUT_MS,
                "max_bytes": max_bytes(4096),
                "newline": {
                    "type": "string",
                    "description": "Line ending to wait for, for this call only; default the connection's.",
                },
                "as": DATA_FORMAT,
            },
            "required": ["connection_id"],
        },
    ),
    # ---- read_until ----
    Tool(
        name="serial.read_until",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use to wait for a prompt or marker, such as '> ' or 'OK': reads until the delimiter arrives, "
            "max_bytes is reached, or the read timeout passes, and returns what it has, including the delimiter "
            "when it arrived."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "delimiter": {
                    "type": "string",
                    "default": "\n",
                    "description": "Text to wait for (default a newline).",
                },
                "timeout_ms": READ_TIMEOUT_MS,
                "max_bytes": max_bytes(4096),
                "as": DATA_FORMAT,
            },
            "required": ["connection_id"],
        },
    ),
    # ---- flush ----
    Tool(
        name="serial.flush",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use to start from a clean state, for example to drop stale output before sending a command whose "
            "reply you want to read alone. Discards pending data in the input buffer, the output buffer, or both. "
            "Flushing output also cancels writes still queued, which return write_aborted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "what": {
                    "type": "string",
                    "enum": ["input", "output", "both"],
                    "default": "both",
                    "description": "Which buffer to discard: input, output, or both (default both).",
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
    """Read from the buffer; ``readline`` and ``read_until`` follow the same rules.

    The wait defaults to the connection's read timeout. A request cancelled during the wait abandons its worker
    thread without stopping it, so the thread can still take bytes that arrive before the timeout, and nobody
    receives them.
    """
    conn = state.get_connection(args["connection_id"])
    nbytes = min(int(args.get("nbytes", 256)), MAX_READ_BYTES)
    fmt = args.get("as", "text")
    timeout_ms = args.get("timeout_ms")
    timeout_s = int(timeout_ms) / 1000.0 if timeout_ms is not None else conn.timeout

    raw = await anyio.to_thread.run_sync(conn.buffer.read, nbytes, timeout_s, abandon_on_cancel=True)

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
            # Not strict: characters outside the base64 alphabet are skipped instead of rejected.
            payload = base64.b64decode(data_str)
        except Exception:
            return _err("invalid_value", "data is not valid base64.")
    else:
        # A character the encoding cannot represent is replaced (with "?" in most codecs), not rejected.
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

    raw = await anyio.to_thread.run_sync(
        conn.buffer.read_until, expected, max_bytes, timeout_s, abandon_on_cancel=True
    )

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

    raw = await anyio.to_thread.run_sync(
        conn.buffer.read_until, expected, max_bytes, timeout_s, abandon_on_cancel=True
    )

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
        await anyio.to_thread.run_sync(conn.ser.reset_input_buffer)
        # A script may have claimed the connection meanwhile; nothing awaits between this check and the discard.
        blocked = script_running_error(state, conn.connection_id)
        if blocked is not None:
            return blocked
        conn.buffer.clear()
    if what in ("output", "both"):
        # Queued and running writes would otherwise refill the buffer being flushed.
        conn.writer.discard_pending()
        await anyio.to_thread.run_sync(conn.ser.reset_output_buffer)

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
