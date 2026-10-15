"""Connection tools: list ports, open, close, configure, and report a connection's status."""

from __future__ import annotations

import time
from functools import partial
from typing import Any

import anyio
import anyio.lowlevel
import anyio.to_thread
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
from serial_mcp_server.mcp.threads import run_to_completion
from serial_mcp_server.mcp.tools.io import PACING_KEYS, parse_write_pacing
from serial_mcp_server.mcp.tools.schemas import CONNECTION_ID, pacing_properties
from serial_mcp_server.port.connection import stop_connections
from serial_mcp_server.port.reader import READ_POLL_MS
from serial_mcp_server.port.stack import PortLogStartError, conn_config, open_port_stack
from serial_mcp_server.portlog.formats import (
    CONFIGURE_EVENT,
    LOG_FORMATS_SCHEMA,
    LOG_TEXT_TIMESTAMPS_SCHEMA,
    is_text_encoding,
)
from serial_mcp_server.portlog.request import resolve_log_request
from serial_mcp_server.script.runner import SCRIPT_CANCEL_WAIT_SECONDS

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


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    # ---- list_ports ----
    Tool(
        name="serial.list_ports",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description=(
            "Use when the user mentions a device but not its port, or a port fails to open: lists the serial "
            "ports on this machine with their description, USB vendor and product IDs, manufacturer, and serial "
            "number, which identify the device."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    # ---- open ----
    Tool(
        name="serial.open",
        # Opening a port can assert DTR/RTS and reset the target.
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use first whenever the user wants to talk to a serial device: opens the port and returns the "
            "connection_id that every other serial tool takes. The port stays open across calls until "
            "serial.close or the server exits. Defaults are 115200 baud, 8N1, and \\r\\n line endings. A wrong "
            "baud rate is the usual cause of garbled text, so take the settings from a protocol spec or the user "
            "when unsure. When the user asks to log or record the session, pass log=true; the response lists the "
            "log files. After opening: 1) check serial.spec.list for the device's protocol spec and attach it "
            "with serial.spec.attach; 2) check serial.plugin.list for plugin tools that fit the device; "
            "3) call serial.read to collect the boot banner or prompt that many devices send on connect."
            " When mirroring is enabled, mirror metadata includes its state and queued-output counters."
            " The lines log format splits long lines into partial/continued records to bound buffering."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "port": {
                    "type": "string",
                    "description": "Port path from serial.list_ports, such as /dev/ttyUSB0 or COM3.",
                },
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
                    "description": "How long a read waits for data by default, in milliseconds (default 200).",
                },
                "write_timeout_ms": {
                    "type": ["integer", "string"],
                    "default": 200,
                    "description": "How long a write may block in the serial layer, in milliseconds (default 200).",
                },
                **pacing_properties(per_call=False),
                "exclusive": {
                    "type": ["boolean", "string"],
                    "description": "Ask the operating system for exclusive access where it supports it.",
                },
                "encoding": {
                    "type": "string",
                    "default": "utf-8",
                    "description": "Text encoding for reads and writes on this connection (default utf-8).",
                },
                "newline": {
                    "type": "string",
                    "default": "\r\n",
                    "description": "Line terminator for serial.readline and append_newline (default \\r\\n).",
                },
                "log": {
                    "type": ["boolean", "string"],
                    "description": (
                        "Set true when the user asks to log or record the session: the server writes the bytes "
                        "on the wire to log files it names, and the response lists their paths. Passing "
                        "log_formats also turns logging on. Set false to skip the logging that SERIAL_MCP_LOG "
                        "turns on by default."
                    ),
                },
                "log_formats": LOG_FORMATS_SCHEMA,
                "log_text_timestamps": LOG_TEXT_TIMESTAMPS_SCHEMA,
            },
            "required": ["port"],
        },
    ),
    # ---- close ----
    Tool(
        name="serial.close",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use when the work with a device is finished, or before another program needs the port: closes the "
            "connection, stops its port log and any script run on it, and releases the port."
        ),
        inputSchema={
            "type": "object",
            "properties": {"connection_id": CONNECTION_ID},
            "required": ["connection_id"],
        },
    ),
    # ---- connection_status ----
    Tool(
        name="serial.connection_status",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use to check one connection without reading from it: whether it is open, its settings, "
            "buffered_bytes waiting to be read, and its log files when logging. dropped_bytes counts received "
            "bytes discarded because the 1 MiB read buffer filled before anything read them; above zero, earlier "
            "output is lost, so read more often. Mirror metadata includes state, failure_reason, pending_bytes, "
            "and undelivered_bytes. A failed mirror leaves serial I/O running; stop any script, then close and "
            "reopen the connection to recover the mirror, which can reset the device."
        ),
        inputSchema={
            "type": "object",
            "properties": {"connection_id": CONNECTION_ID},
            "required": ["connection_id"],
        },
    ),
    # ---- configure ----
    Tool(
        name="serial.configure",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        description=(
            "Use to change write pacing, text encoding, or newline on an open connection, instead of closing and "
            "reopening, which can toggle DTR/RTS and reset the device. Pass pacing arguments as a complete set, "
            "as on serial.write; they replace the whole pacing mode. A change applies to writes submitted "
            "afterwards."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                **pacing_properties(per_call=False),
                "encoding": {
                    "type": "string",
                    "description": "Text encoding for reads and writes, such as utf-8.",
                },
                "newline": {
                    "type": "string",
                    "description": "Line terminator for serial.readline and append_newline, such as \\r\\n.",
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
    ports = await anyio.to_thread.run_sync(serial.tools.list_ports.comports, abandon_on_cancel=True)
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

    # Check for duplicate port. This knows only this server's connections: another program can hold the port,
    # and without ``exclusive`` pyserial opens it shared.
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
        "timeout": READ_POLL_MS / 1000.0,  # timeout_s stays the read tools' default wait; see READ_POLL_MS
        "write_timeout": write_timeout_s,
    }
    if exclusive is not None:
        kwargs["exclusive"] = exclusive

    # Both threads build something that holds the port, so a cancelled request waits for them rather than
    # abandoning them, then releases what they built; the cancellation arrives at the next checkpoint.
    ser = await anyio.to_thread.run_sync(partial(pyserial.Serial, **kwargs))
    build_stack = partial(
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
        mirror_link=state.settings.mirror_link,
    )
    try:
        conn = await anyio.to_thread.run_sync(build_stack)
    except PortLogStartError as exc:
        await run_to_completion(ser.close)
        return _err("log_failed", str(exc))
    except BaseException:
        await run_to_completion(ser.close)
        raise
    try:
        await anyio.lowlevel.checkpoint()  # a request cancelled while the stack was built ends here
        state.add_connection(conn)
    except BaseException:
        await run_to_completion(stop_connections, [conn])  # joins the threads, closes the log and the port
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
        # Waits up to 5 s for the runner. If it has not stopped by then, the connection is closed anyway, and
        # the runner finishes against a stopped writer.
        await anyio.to_thread.run_sync(run.runner.join, SCRIPT_CANCEL_WAIT_SECONDS, abandon_on_cancel=True)
    # Remove the connection on the loop, then join its threads off it.
    conn = state.remove_connection(connection_id)
    await run_to_completion(stop_connections, [conn])  # removed above, so it must be stopped now
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


# ---------------------------------------------------------------------------
# Handler map
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Any] = {
    "serial.list_ports": handle_list_ports,
    "serial.open": handle_open,
    "serial.close": handle_close,
    "serial.connection_status": handle_connection_status,
    "serial.configure": handle_configure,
}
