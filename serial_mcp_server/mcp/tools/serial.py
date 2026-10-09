"""Serial tool definitions and handlers — list ports, open, read, write, control lines."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import serial as pyserial
import serial.tools.list_ports
from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.responses import (
    _coerce_bool,
    _err,
    _ok,
    script_running_error,
)
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.writes import duration_error, run_write_job
from serial_mcp_server.port.connection import SerialConnection, stop_connections
from serial_mcp_server.port.pacing import (
    MAX_PACING_DELAY_MS,
    NO_PACING,
    Pacing,
    paced_transfer_seconds,
)
from serial_mcp_server.port.stack import PortLogStartError, conn_config, open_port_stack
from serial_mcp_server.portlog.formats import (
    CONFIGURE_EVENT,
    LOG_FORMATS_SCHEMA,
    LOG_TEXT_TIMESTAMPS_SCHEMA,
    is_text_encoding,
)
from serial_mcp_server.portlog.request import resolve_log_request
from serial_mcp_server.script.runner import SCRIPT_CANCEL_WAIT_SECONDS

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


def _release_when_worker_ends(worker: asyncio.Future[SerialConnection], ser: pyserial.Serial) -> None:
    """Release what an abandoned ``open_port_stack`` worker built, once its thread ends.

    Cancelling ``serial.open`` cannot stop the worker thread, so closing *ser* now would pull the port
    out from under it. This waits for the thread, then stops the connection it returned or, if it failed,
    closes the port. The blocking release runs in the default executor, never on the event loop.
    """

    def release(finished: asyncio.Future[SerialConnection]) -> None:
        loop = finished.get_loop()
        if finished.cancelled() or finished.exception() is not None:
            loop.run_in_executor(None, ser.close)
        else:
            loop.run_in_executor(None, stop_connections, [finished.result()])

    worker.add_done_callback(release)


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
            state.settings,
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
    worker = asyncio.ensure_future(
        asyncio.to_thread(
            open_port_stack,
            ser,
            port=port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout_s,
            write_timeout=write_timeout_s,
            encoding=encoding,
            newline=newline,
            pacing=pacing,
            log_request=log_request,
            connection_id=state.generate_id(),
            mirror_mode=state.settings.mirror,
            mirror_link=str(state.settings.mirror_link) if state.settings.mirror_link else None,
        )
    )
    try:
        conn = await asyncio.shield(worker)
    except asyncio.CancelledError:
        _release_when_worker_ends(worker, ser)  # the thread keeps running and owns *ser* until it ends
        raise
    except PortLogStartError as exc:
        await asyncio.to_thread(ser.close)
        return _err("log_failed", str(exc))
    except BaseException:
        await asyncio.to_thread(ser.close)
        raise
    try:
        state.add_connection(conn)
    except BaseException:
        await asyncio.to_thread(stop_connections, [conn])  # joins the threads, closes the log and the port
        raise

    result = _ok(
        message=f"Opened {port} at {baudrate} baud.",
        connection_id=conn.connection_id,
        config=conn_config(conn),
    )
    if conn.log_slot.log is not None:
        result["log"] = conn.log_slot.log.status()
    mirror = conn.reader.mirror_info() if conn.reader is not None else None
    if mirror is not None:
        result["mirror"] = mirror
    return result


async def handle_close(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    connection_id = args["connection_id"]
    state.get_connection(connection_id)  # raises not_found before any script run is touched
    run = state.script_jobs.active_for_connection(connection_id)
    if run is not None:
        run.runner.cancel()
        await asyncio.to_thread(run.runner.join, SCRIPT_CANCEL_WAIT_SECONDS)
    # Remove the connection on the loop, then join its threads off it.
    conn = state.remove_connection(connection_id)
    await asyncio.to_thread(stop_connections, [conn])
    return _ok(message=f"Closed {conn.port}.", connection_id=connection_id, port=conn.port)


async def handle_configure(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
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


async def handle_set_dtr(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
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
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
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
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    duration_ms = min(int(args.get("duration_ms", 100)), _MAX_PULSE_MS)
    conn.ser.dtr = False
    conn.log_slot.event("dtr", value=False)
    await asyncio.sleep(duration_ms / 1000.0)
    # No claim check: the pulse began before any claim; a line left low under a script is worse than one edge.
    conn.ser.dtr = True
    conn.log_slot.event("dtr", value=True)
    conn.last_seen_ts = time.time()
    return _ok(message=f"Pulsed DTR low for {duration_ms}ms on {conn.port}.", duration_ms=duration_ms)


async def handle_pulse_rts(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    duration_ms = min(int(args.get("duration_ms", 100)), _MAX_PULSE_MS)
    conn.ser.rts = False
    conn.log_slot.event("rts", value=False)
    await asyncio.sleep(duration_ms / 1000.0)
    # No claim check: the pulse began before any claim; a line left low under a script is worse than one edge.
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
