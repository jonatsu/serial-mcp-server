"""Port log tool definitions and handlers — start, stop, status, mark."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.helpers import _err, _ok, script_running_error
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.stack import conn_config
from serial_mcp_server.portlog import PortLog, build_log_request
from serial_mcp_server.portlog_formats import (
    CLOSE_EVENT,
    LOG_FORMATS_SCHEMA,
    LOG_TEXT_TIMESTAMPS_SCHEMA,
    MARK_EVENT,
    OPEN_EVENT,
)
from serial_mcp_server.state import SerialState

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_CONNECTION_ID = {"type": "string"}
_NOT_LOGGING_HINT = "Call serial.log.start to begin logging."

TOOLS: list[Tool] = [
    Tool(
        name="serial.log.start",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        description=(
            "Start recording an open connection's bytes on the wire to new port log files. "
            "Use it when the user asks to log or record a session that is already running; "
            "serial.open with log=true does the same at open time. "
            "The server names the files, and the response lists their paths. "
            "A connection that is already logging returns already_logging."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": _CONNECTION_ID,
                "log_formats": LOG_FORMATS_SCHEMA,
                "log_text_timestamps": LOG_TEXT_TIMESTAMPS_SCHEMA,
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="serial.log.stop",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        description=(
            "Stop the port log: flush and close its files, and return their paths and sizes. "
            "The connection stays open."
        ),
        inputSchema={
            "type": "object",
            "properties": {"connection_id": _CONNECTION_ID},
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="serial.log.status",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Report whether a connection is logging, with its formats, file paths, entry count, "
            "and the bytes dropped because the disk fell behind."
        ),
        inputSchema={
            "type": "object",
            "properties": {"connection_id": _CONNECTION_ID},
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="serial.log.mark",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        description=(
            "Write a labelled marker into the port log between the surrounding traffic, "
            "for example at the start of a test step. "
            "The jsonl and lines logs carry markers; the text and raw logs stay byte-exact."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": _CONNECTION_ID,
                "label": {"type": "string", "description": "Short text to record, such as 'step 2: boot'."},
            },
            "required": ["connection_id", "label"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _already_logging(conn: SerialConnection) -> dict[str, Any]:
    return _err("already_logging", f"{conn.port} is already logging. Call serial.log.stop first.")


def _not_logging(conn: SerialConnection) -> dict[str, Any]:
    return _err("not_logging", f"{conn.port} is not logging. {_NOT_LOGGING_HINT}")


async def handle_log_start(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    if conn.log_slot.log is not None:
        return _already_logging(conn)
    try:
        request = build_log_request(state.settings, args.get("log_formats"), args.get("log_text_timestamps"))
    except ValueError as exc:
        return _err("invalid_params", str(exc))

    try:
        log = await asyncio.to_thread(PortLog.start, request, conn.encoding)
    except OSError as exc:
        return _err("log_failed", f"Could not start the port log in {request.directory}: {exc}")
    # Nothing below awaits until the log is attached, so these checks and the attach are one step.
    if state.connections.get(conn.connection_id) is not conn:
        await _discard(log)
        return _err("not_found", f"Connection {conn.connection_id} closed while the log was starting.")
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        await _discard(log)
        return blocked
    if conn.log_slot.log is not None:  # another start finished while the files were being created
        await _discard(log)
        return _already_logging(conn)
    log.event(OPEN_EVENT, config=conn_config(conn))  # queued before the log is attached, so it comes first
    conn.log_slot.set(log)
    return _ok(message=f"Logging {conn.port}.", connection_id=conn.connection_id, log=log.status())


async def _discard(log: PortLog) -> None:
    """Close a log that was never attached and remove its files."""
    await asyncio.to_thread(log.discard)


async def handle_log_stop(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    log = conn.log_slot.detach()
    if log is None:
        return _not_logging(conn)
    log.event(CLOSE_EVENT)
    await asyncio.to_thread(log.close)
    status = log.status()
    files = [
        {"format": name, "path": str(path), "bytes": path.stat().st_size if path.exists() else 0}
        for name, path in log.files.items()
    ]
    return _ok(
        message=f"Stopped logging {conn.port}.",
        connection_id=conn.connection_id,
        files=files,
        entries=status["entries"],
        dropped_bytes=status["dropped_bytes"],
    )


async def handle_log_status(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    log = conn.log_slot.log
    if log is None:
        return _ok(message=f"{conn.port} is not logging.", connection_id=conn.connection_id, logging=False)
    return _ok(
        message=f"{conn.port} is logging.", connection_id=conn.connection_id, logging=True, **log.status()
    )


async def handle_log_mark(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    label = args["label"]
    if not isinstance(label, str):
        return _err("invalid_params", "label must be a string.")
    if conn.log_slot.log is None:
        return _not_logging(conn)
    conn.log_slot.event(MARK_EVENT, label=label)
    return _ok(message=f"Marked {conn.port}.", connection_id=conn.connection_id, label=label)


HANDLERS: dict[str, Any] = {
    "serial.log.start": handle_log_start,
    "serial.log.stop": handle_log_stop,
    "serial.log.status": handle_log_status,
    "serial.log.mark": handle_log_mark,
}
