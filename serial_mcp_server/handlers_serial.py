"""Serial tool definitions and handlers — list ports, open, read, write, control lines."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import serial as pyserial
import serial.tools.list_ports
from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.helpers import (
    MIRROR_PTY,
    MIRROR_PTY_LINK,
    _coerce_bool,
    _err,
    _ok,
    max_write_seconds,
)
from serial_mcp_server.mirror import SerialBuffer, create_reader
from serial_mcp_server.portlog import PortLog, PortLogSlot, resolve_log_request
from serial_mcp_server.portlog_formats import (
    BUFFER_DROP_EVENT,
    CONFIGURE_EVENT,
    LOG_FORMATS_SCHEMA,
    LOG_TEXT_TIMESTAMPS_SCHEMA,
    OPEN_EVENT,
    is_text_encoding,
)
from serial_mcp_server.state import SerialConnection, SerialState, stop_connections
from serial_mcp_server.writer import NO_PACING, Pacing, SerialWriter, WriteAborted, WriterClosed

logger = logging.getLogger("serial_mcp_server")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PARITY_MAP = {
    "N": pyserial.PARITY_NONE,
    "E": pyserial.PARITY_EVEN,
    "O": pyserial.PARITY_ODD,
    "M": pyserial.PARITY_MARK,
    "S": pyserial.PARITY_SPACE,
}

STOPBITS_MAP = {
    1: pyserial.STOPBITS_ONE,
    1.5: pyserial.STOPBITS_ONE_POINT_FIVE,
    2: pyserial.STOPBITS_TWO,
}

BYTESIZE_MAP = {
    5: pyserial.FIVEBITS,
    6: pyserial.SIXBITS,
    7: pyserial.SEVENBITS,
    8: pyserial.EIGHTBITS,
}

# Maximum read size to prevent accidental huge allocations.
MAX_READ_BYTES = 1_048_576  # 1 MiB
MAX_WRITE_DELAY_MS = 1000
BITS_PER_BYTE_ON_WIRE = 10  # start bit, 8 data bits, stop bit
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


def paced_transfer_seconds(pacing: Pacing, size: int, baudrate: int) -> float:
    """Estimated time of a paced transfer: the larger of the pauses between segments and the time on the wire.

    The larger of the two, because large chunks make the pauses negligible while the line stays busy.
    Zero when unpaced, because the cap limits pacing, not the speed of the line.
    """
    if not pacing.is_paced:
        return 0.0
    return max(pacing.estimated_seconds(size), wire_seconds(size, baudrate))


def duration_error(estimate_seconds: float) -> dict[str, Any] | None:
    """Return an error when an estimated transfer time exceeds the server-wide cap."""
    if estimate_seconds <= 0:
        return None
    cap = max_write_seconds()
    if estimate_seconds <= cap:
        return None
    return _err(
        "invalid_params",
        f"Estimated write time {estimate_seconds:.1f} s exceeds the {cap:g} s cap "
        "(SERIAL_MCP_MAX_WRITE_SECONDS). Send less data per call or reduce the pacing delay.",
    )


def wire_seconds(size: int, baudrate: int) -> float:
    """Time *size* bytes take on the wire at *baudrate*, with ten bits per byte."""
    return size * BITS_PER_BYTE_ON_WIRE / baudrate if baudrate > 0 else 0.0


def _bytes_written_by(future: Future[int]) -> int:
    if future.cancelled():
        return 0
    error = future.exception()
    if isinstance(error, WriteAborted):
        return error.bytes_written
    return 0 if error is not None else future.result()


async def run_write_job(
    conn: SerialConnection,
    payload: bytes,
    pacing: Pacing,
    on_done: Callable[[int], None] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Queue *payload* on the connection's writer and wait for it.

    *on_done* is called exactly once with the bytes written, after the writer has stopped
    touching the port, so anything it logs follows the write's own port-log entries. It runs
    on the writer's thread, or on the caller's when the write never started.

    Returns:
        The bytes written and an error response, which is None on success. A cancelled request
        stops the write between segments and re-raises.
    """
    try:
        job = conn.writer.submit(payload, pacing)
    except WriterClosed as exc:
        if on_done is not None:
            on_done(0)
        return 0, _err("connection_closed", str(exc))
    if on_done is not None:
        job.future.add_done_callback(lambda future: on_done(_bytes_written_by(future)))
    try:
        return await asyncio.wrap_future(job.future), None
    except asyncio.CancelledError:
        job.cancel.set()
        raise
    except WriterClosed as exc:
        return 0, _err("connection_closed", str(exc))
    except WriteAborted as exc:
        return exc.bytes_written, {**_err("write_aborted", str(exc)), "bytes_written": exc.bytes_written}


def make_read_buffer(slot: PortLogSlot) -> SerialBuffer:
    """A read buffer that records each trim of unread bytes as a ``buffer_drop`` port-log event."""
    return SerialBuffer(on_drop=lambda count: slot.event(BUFFER_DROP_EVENT, dropped_bytes=count))


def conn_config(conn: SerialConnection) -> dict[str, Any]:
    """Return a dict describing the connection's serial configuration."""
    pacing = conn.writer.pacing
    return {
        "port": conn.port,
        "baudrate": conn.baudrate,
        "bytesize": conn.bytesize,
        "parity": conn.parity,
        "stopbits": conn.stopbits,
        "timeout_ms": round(conn.timeout * 1000),
        "write_timeout_ms": round(conn.write_timeout * 1000),
        "inter_char_delay_ms": pacing.inter_char_delay_ms,
        "chunk_size": pacing.chunk_size,
        "chunk_delay_ms": pacing.chunk_delay_ms,
        "encoding": conn.encoding,
        "newline": repr(conn.newline),
    }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    # ---- 1. list_ports ----
    Tool(
        name="serial.list_ports",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description="List available serial ports on the system.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    # ---- 2. open ----
    Tool(
        name="serial.open",
        # Opening a port can assert DTR/RTS and reset the target.
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Open a serial port connection. Returns a connection_id for use with other serial tools. "
            "The port stays open across tool calls until serial.close is called or the server exits. "
            "Defaults are 115200 baud, 8N1, \\r\\n line terminator — the most common settings. "
            "When the user asks to log or record the session, pass log=true; the response lists the log file paths. "
            "If you don't know the correct settings, check for a protocol spec with serial.spec.list "
            "or ask the user. Wrong baud rate is the most common cause of garbled data. "
            "After opening: 1) Use serial.spec.list to check for a matching protocol spec. "
            "If a match is found, attach it with serial.spec.attach. "
            "2) Use serial.plugin.list to check for a plugin that matches the device. "
            "If a matching plugin is loaded, its tools are available to use directly. "
            "3) Do a serial.read to check for any buffered data — many devices "
            "send a boot banner, prompt, or status message on connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "port": {"type": "string", "description": "Serial port path (e.g. /dev/ttyUSB0, COM3)."},
                "baudrate": {
                    "type": ["integer", "string"],
                    "default": 115200,
                    "description": "Baud rate (default 115200). Common values: 9600, 19200, 38400, 57600, 115200.",
                },
                "bytesize": {
                    "type": ["integer", "string"],
                    "enum": [5, 6, 7, 8],
                    "default": 8,
                    "description": "Data bits (default 8).",
                },
                "parity": {
                    "type": "string",
                    "enum": ["N", "E", "O", "M", "S"],
                    "default": "N",
                    "description": "Parity: N(one), E(ven), O(dd), M(ark), S(pace). Default N.",
                },
                "stopbits": {
                    "type": ["number", "string"],
                    "enum": [1, 1.5, 2],
                    "default": 1,
                    "description": "Stop bits (default 1).",
                },
                "timeout_ms": {
                    "type": ["integer", "string"],
                    "default": 200,
                    "description": "Read timeout in milliseconds (default 200).",
                },
                "write_timeout_ms": {
                    "type": ["integer", "string"],
                    "default": 200,
                    "description": "Write timeout in milliseconds (default 200).",
                },
                "inter_char_delay_ms": {
                    "type": ["integer", "string"],
                    "default": 0,
                    "description": (
                        "Delay between transmitted characters in milliseconds (0 disables pacing; max 1000). "
                        "Applies to serial.write and read/write PTY forwarding. "
                        "Cannot be combined with chunk pacing."
                    ),
                },
                "chunk_size": {
                    "type": ["integer", "string"],
                    "default": 0,
                    "description": "Bytes per chunk in chunk pacing mode; set with chunk_delay_ms (0 disables).",
                },
                "chunk_delay_ms": {
                    "type": ["integer", "string"],
                    "default": 0,
                    "description": "Delay after each drained chunk in milliseconds (0 disables; max 1000).",
                },
                "exclusive": {
                    "type": ["boolean", "string"],
                    "description": "Request exclusive access (platform-dependent, ignored if unsupported).",
                },
                "encoding": {
                    "type": "string",
                    "default": "utf-8",
                    "description": "Default text encoding for this connection (default utf-8).",
                },
                "newline": {
                    "type": "string",
                    "default": "\\r\\n",
                    "description": "Default line terminator for readline and append_newline (default \\r\\n).",
                },
                "log": {
                    "type": ["boolean", "string"],
                    "description": (
                        "Set true when the user asks to log or record the session: the server writes the bytes "
                        "on the wire to log files it names, and the response lists their paths. "
                        "Set false to skip logging that SERIAL_MCP_LOG would enable."
                    ),
                },
                "log_formats": LOG_FORMATS_SCHEMA,
                "log_text_timestamps": LOG_TEXT_TIMESTAMPS_SCHEMA,
            },
            "required": ["port"],
        },
    ),
    # ---- 3. close ----
    Tool(
        name="serial.close",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description="Close a serial port connection and release the port.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string", "description": "The connection_id from serial.open."},
            },
            "required": ["connection_id"],
        },
    ),
    # ---- 4. connection_status ----
    Tool(
        name="serial.connection_status",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Check whether a connection is open and return its configuration, buffered_bytes, and log files "
            "when logging. dropped_bytes counts received bytes discarded because the 1 MiB read buffer filled "
            "before anything read them; above zero, earlier output is lost, so read more often."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string", "description": "The connection_id from serial.open."},
            },
            "required": ["connection_id"],
        },
    ),
    # ---- 4b. configure ----
    Tool(
        name="serial.configure",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        description=(
            "Reconfigure an open connection in place: write pacing, text encoding, or newline. "
            "Use it instead of closing and reopening, which can toggle DTR/RTS and reset the device. "
            "Pass pacing arguments as a complete set, as on serial.write; they replace the whole pacing mode. "
            "A change applies to writes submitted afterwards."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "inter_char_delay_ms": {
                    "type": ["integer", "string"],
                    "description": "Delay between transmitted characters in milliseconds (0 disables; max 1000).",
                },
                "chunk_size": {
                    "type": ["integer", "string"],
                    "description": "Bytes per chunk in chunk pacing mode; set with chunk_delay_ms (0 disables).",
                },
                "chunk_delay_ms": {
                    "type": ["integer", "string"],
                    "description": "Delay after each drained chunk in milliseconds (0 disables; max 1000).",
                },
                "encoding": {
                    "type": "string",
                    "description": "Text encoding for reads and writes, such as utf-8.",
                },
                "newline": {
                    "type": "string",
                    "description": "Default line terminator for serial.readline and append_newline, such as \\r\\n.",
                },
            },
            "required": ["connection_id"],
        },
    ),
    # ---- 5. read ----
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
    # ---- 6. write ----
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
    # ---- 7. readline ----
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
    # ---- 8. read_until ----
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
    # ---- 9. flush ----
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
    # ---- 10. set_dtr ----
    Tool(
        name="serial.set_dtr",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description="Set the DTR (Data Terminal Ready) control line. Usage is device-specific — check the protocol spec or ask the user.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "value": {"type": ["boolean", "string"], "description": "True = high, False = low."},
            },
            "required": ["connection_id", "value"],
        },
    ),
    # ---- 11. set_rts ----
    Tool(
        name="serial.set_rts",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description="Set the RTS (Request To Send) control line. Usage is device-specific — check the protocol spec or ask the user.",
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "value": {"type": ["boolean", "string"], "description": "True = high, False = low."},
            },
            "required": ["connection_id", "value"],
        },
    ),
    # ---- 12. pulse_dtr ----
    Tool(
        name="serial.pulse_dtr",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Pulse the DTR line: sets low, waits duration_ms, then sets high. "
            "Commonly used to reset microcontrollers (e.g. Arduino, ESP32). "
            "Check the protocol spec or ask the user before pulsing — effect is device-specific."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "duration_ms": {
                    "type": ["integer", "string"],
                    "default": 100,
                    "description": "Pulse duration in milliseconds (default 100).",
                },
            },
            "required": ["connection_id"],
        },
    ),
    # ---- 13. pulse_rts ----
    Tool(
        name="serial.pulse_rts",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Pulse the RTS line: sets low, waits duration_ms, then sets high. "
            "Some devices use RTS to enter bootloader mode. "
            "Check the protocol spec or ask the user before pulsing — effect is device-specific."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "duration_ms": {
                    "type": ["integer", "string"],
                    "default": 100,
                    "description": "Pulse duration in milliseconds (default 100).",
                },
            },
            "required": ["connection_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_list_ports(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    ports = await asyncio.to_thread(serial.tools.list_ports.comports)
    port_list = []
    for p in sorted(ports, key=lambda x: x.device):
        info: dict[str, Any] = {"device": p.device, "description": p.description, "hwid": p.hwid}
        if p.name:
            info["name"] = p.name
        if p.vid is not None:
            info["vid"] = f"0x{p.vid:04X}"
        if p.pid is not None:
            info["pid"] = f"0x{p.pid:04X}"
        if p.serial_number:
            info["serial_number"] = p.serial_number
        if p.manufacturer:
            info["manufacturer"] = p.manufacturer
        if p.product:
            info["product"] = p.product
        if p.location:
            info["location"] = p.location
        port_list.append(info)
    return _ok(
        message=f"Found {len(port_list)} serial port(s).",
        ports=port_list,
        count=len(port_list),
    )


def _abandon_open(ser: pyserial.Serial, slot: PortLogSlot, conn: SerialConnection | None) -> None:
    """Release what ``handle_open`` acquired when the connection was never registered."""
    if conn is not None:
        stop_connections([conn])  # joins the threads, closes the log and the port
        return
    slot.close_log()
    ser.close()


async def handle_open(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    port = args["port"]
    baudrate = int(args.get("baudrate", 115200))
    bytesize = int(args.get("bytesize", 8))
    parity = args.get("parity", "N")
    stopbits = float(args.get("stopbits", 1))
    timeout_ms = int(args.get("timeout_ms", 200))
    write_timeout_ms = int(args.get("write_timeout_ms", 200))
    try:
        pacing = parse_write_pacing(args)
    except ValueError as exc:
        return _err("invalid_params", str(exc))
    exclusive = _coerce_bool(args["exclusive"]) if "exclusive" in args else None
    encoding = args.get("encoding", "utf-8")
    newline = args.get("newline", "\r\n")

    # Validate parameters
    if not is_text_encoding(encoding):
        return _err("invalid_params", f"Unknown text encoding {encoding!r}.")
    if parity not in PARITY_MAP:
        return _err("invalid_params", f"Invalid parity '{parity}'. Must be one of: N, E, O, M, S.")
    if stopbits not in STOPBITS_MAP:
        return _err("invalid_params", f"Invalid stopbits '{stopbits}'. Must be one of: 1, 1.5, 2.")
    if bytesize not in BYTESIZE_MAP:
        return _err("invalid_params", f"Invalid bytesize '{bytesize}'. Must be one of: 5, 6, 7, 8.")
    if timeout_ms < 0 or write_timeout_ms < 0:
        return _err("invalid_params", "Timeouts must be non-negative.")

    # Check for duplicate port
    for conn in state.connections.values():
        if conn.port == port and conn.ser.is_open:
            return _err(
                "already_open",
                f"Port {port} is already open as connection {conn.connection_id}.",
            )

    try:
        log_request = resolve_log_request(
            _coerce_bool(args["log"]) if "log" in args else None,
            args.get("log_formats"),
            args.get("log_text_timestamps"),
        )
    except ValueError as exc:
        return _err("invalid_params", str(exc))

    timeout_s = timeout_ms / 1000.0
    write_timeout_s = write_timeout_ms / 1000.0

    kwargs: dict[str, Any] = {
        "port": port,
        "baudrate": baudrate,
        "bytesize": BYTESIZE_MAP[bytesize],
        "parity": PARITY_MAP[parity],
        "stopbits": STOPBITS_MAP[stopbits],
        "timeout": timeout_s,
        "write_timeout": write_timeout_s,
    }
    if exclusive is not None:
        kwargs["exclusive"] = exclusive

    ser = await asyncio.to_thread(pyserial.Serial, **kwargs)

    slot = PortLogSlot()
    conn: SerialConnection | None = None
    registered = False
    try:
        if log_request is not None:
            # Fail the open rather than run a session the caller asked to record without its log.
            try:
                slot.set(await asyncio.to_thread(PortLog.start, log_request, encoding))
            except OSError as exc:
                return _err("log_failed", f"Could not start the port log in {log_request.directory}: {exc}")

        buf = make_read_buffer(slot)
        writer = SerialWriter(ser, pacing, slot)
        reader = create_reader(ser, buf, MIRROR_PTY, MIRROR_PTY_LINK, writer=writer, log_slot=slot)
        conn = SerialConnection(
            connection_id=state.generate_id(),
            port=port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout_s,
            write_timeout=write_timeout_s,
            encoding=encoding,
            newline=newline,
            ser=ser,
            writer=writer,
            buffer=buf,
            reader=reader,
        )
        # Record the open before the threads start, so the log begins with it, not with a boot banner.
        slot.event(OPEN_EVENT, config=conn_config(conn))
        writer.start()
        reader.start()
        state.add_connection(conn)
        registered = True
    finally:
        if not registered:
            await asyncio.to_thread(_abandon_open, ser, slot, conn)

    result = _ok(
        message=f"Opened {port} at {baudrate} baud.",
        connection_id=conn.connection_id,
        config=conn_config(conn),
    )
    if slot.log is not None:
        result["log"] = slot.log.status()
    mirror = reader.mirror_info()
    if mirror is not None:
        result["mirror"] = mirror
    return result


async def handle_close(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    connection_id = args["connection_id"]
    # Remove the connection on the loop, then join its threads off it.
    conn = state.remove_connection(connection_id)
    await asyncio.to_thread(stop_connections, [conn])
    return _ok(message=f"Closed {conn.port}.", connection_id=connection_id, port=conn.port)


async def handle_configure(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    settings = ("encoding", "newline", *PACING_KEYS)
    if not any(key in args for key in settings):
        return _err("invalid_params", f"Pass at least one of: {', '.join(settings)}.")

    # Validate everything before changing anything, so a bad argument leaves the connection as it was.
    try:
        pacing = parse_write_pacing(args, conn.writer.pacing)
    except ValueError as exc:
        return _err("invalid_params", str(exc))
    encoding = args.get("encoding", conn.encoding)
    if not isinstance(encoding, str) or not is_text_encoding(encoding):
        return _err("invalid_params", f"Unknown text encoding {encoding!r}.")
    newline = args.get("newline", conn.newline)
    if not isinstance(newline, str) or not newline:
        return _err("invalid_params", "newline must be a non-empty string.")

    conn.writer.pacing = pacing
    conn.encoding = encoding
    conn.newline = newline
    conn.log_slot.event(
        CONFIGURE_EVENT,
        inter_char_delay_ms=pacing.inter_char_delay_ms,
        chunk_size=pacing.chunk_size,
        chunk_delay_ms=pacing.chunk_delay_ms,
        encoding=encoding,
        newline=newline,
    )
    conn.last_seen_ts = time.time()
    return _ok(message=f"Configured {conn.port}.", config=conn_config(conn))


async def handle_connection_status(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    is_open = conn.ser.is_open
    result: dict[str, Any] = {
        "connection_id": conn.connection_id,
        "is_open": is_open,
        "config": conn_config(conn),
        "opened_at": conn.opened_at,
        "last_seen_ts": conn.last_seen_ts,
        "buffered_bytes": conn.buffer.available,
        "dropped_bytes": conn.buffer.dropped_bytes,
    }
    if conn.log_slot.log is not None:
        result["log"] = conn.log_slot.log.status()
    if conn.reader is not None:
        mirror = conn.reader.mirror_info()
        if mirror is not None:
            result["mirror"] = mirror
    return _ok(message=f"{conn.port} is {'open' if is_open else 'closed'}.", **result)


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

    too_long = duration_error(paced_transfer_seconds(pacing, len(payload), conn.baudrate))
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
    what = args.get("what", "both")

    if what in ("input", "both"):
        await asyncio.to_thread(conn.ser.reset_input_buffer)
        conn.buffer.clear()
    if what in ("output", "both"):
        # Queued and running writes would otherwise refill the buffer being flushed.
        conn.writer.discard_pending()
        await asyncio.to_thread(conn.ser.reset_output_buffer)

    conn.log_slot.event("flush", what=what)

    conn.last_seen_ts = time.time()
    return _ok(message=f"Flushed {what} buffer(s) on {conn.port}.")


async def handle_set_dtr(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    value = _coerce_bool(args["value"])
    conn.ser.dtr = value
    conn.log_slot.event("dtr", value=value)
    conn.last_seen_ts = time.time()
    return _ok(
        message=f"DTR {'high' if value else 'low'} on {conn.port}.",
        dtr=value,
    )


async def handle_set_rts(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    value = _coerce_bool(args["value"])
    conn.ser.rts = value
    conn.log_slot.event("rts", value=value)
    conn.last_seen_ts = time.time()
    return _ok(
        message=f"RTS {'high' if value else 'low'} on {conn.port}.",
        rts=value,
    )


_MAX_PULSE_MS = 10_000


async def handle_pulse_dtr(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    duration_ms = min(int(args.get("duration_ms", 100)), _MAX_PULSE_MS)
    conn.ser.dtr = False
    conn.log_slot.event("dtr", value=False)
    await asyncio.sleep(duration_ms / 1000.0)
    conn.ser.dtr = True
    conn.log_slot.event("dtr", value=True)
    conn.last_seen_ts = time.time()
    return _ok(message=f"Pulsed DTR low for {duration_ms}ms on {conn.port}.", duration_ms=duration_ms)


async def handle_pulse_rts(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    duration_ms = min(int(args.get("duration_ms", 100)), _MAX_PULSE_MS)
    conn.ser.rts = False
    conn.log_slot.event("rts", value=False)
    await asyncio.sleep(duration_ms / 1000.0)
    conn.ser.rts = True
    conn.log_slot.event("rts", value=True)
    conn.last_seen_ts = time.time()
    return _ok(message=f"Pulsed RTS low for {duration_ms}ms on {conn.port}.", duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Handler map
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Any] = {
    "serial.list_ports": handle_list_ports,
    "serial.open": handle_open,
    "serial.close": handle_close,
    "serial.connection_status": handle_connection_status,
    "serial.configure": handle_configure,
    "serial.read": handle_read,
    "serial.write": handle_write,
    "serial.readline": handle_readline,
    "serial.read_until": handle_read_until,
    "serial.flush": handle_flush,
    "serial.set_dtr": handle_set_dtr,
    "serial.set_rts": handle_set_rts,
    "serial.pulse_dtr": handle_pulse_dtr,
    "serial.pulse_rts": handle_pulse_rts,
}
