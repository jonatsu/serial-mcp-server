"""Port log tool definitions and handlers — start, stop, status, mark."""

from __future__ import annotations

from typing import Any

import anyio
import anyio.lowlevel
import anyio.to_thread
from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.responses import _err, _ok, script_running_error
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.threads import run_to_completion
from serial_mcp_server.mcp.tools.schemas import CONNECTION_ID
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.stack import conn_config
from serial_mcp_server.portlog.formats import (
    CLOSE_EVENT,
    LOG_FORMATS_SCHEMA,
    LOG_TEXT_TIMESTAMPS_SCHEMA,
    MARK_EVENT,
    OPEN_EVENT,
)
from serial_mcp_server.portlog.log import PortLog
from serial_mcp_server.portlog.request import build_log_request

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

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
            "The lines format splits long lines into partial/continued records to bound buffering. "
            "A connection that already has a log returns already_logging. "
            "To recover from admission_error, stop that log and start a new one on the same connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
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
            "Use when the user wants the recording to end, or to hand them finished log files: flushes and "
            "closes the port log's files and returns their paths, sizes, loss counters, and admission_error. "
            "The connection stays open; stopping also releases a log whose admission failed."
        ),
        inputSchema={
            "type": "object",
            "properties": {"connection_id": CONNECTION_ID},
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="serial.log.status",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use to find a connection's log files or check that logging works: reports whether it is logging, "
            "with its formats, file paths, completed entry count, dropped_bytes, dropped_entries, "
            "dropped_events, and admission_error. Loss counters include rejected metadata and pressure "
            "against retained payload, entry, and metadata limits. An admission_error means no further "
            "traffic is admitted; stop and restart logging to recover while keeping the serial connection open."
        ),
        inputSchema={
            "type": "object",
            "properties": {"connection_id": CONNECTION_ID},
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="serial.log.mark",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
        description=(
            "Use to make a log easy to navigate, for example at the start of each test step or before a "
            "command whose output matters: writes a labelled marker into the port log between the surrounding "
            "traffic. "
            "The jsonl and lines logs carry markers; the text and raw logs skip markers. "
            "Returns log_failed if bounded admission rejects the marker, without claiming it was recorded."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "label": {
                    "type": "string",
                    "description": "Short text to record, such as 'step 2: boot'; oversized metadata is rejected.",
                },
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
        # Not abandoned on cancellation: the log's files are open once it starts, so it is attached or
        # discarded below rather than left with no owner.
        log = await anyio.to_thread.run_sync(PortLog.start, request, conn.encoding)
    except OSError as exc:
        return _err("log_failed", f"Could not start the port log in {request.directory}: {exc}")
    try:
        await anyio.lowlevel.checkpoint()  # a request cancelled while the files were created ends here
    except anyio.get_cancelled_exc_class():
        await _discard(log)
        raise
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
    await run_to_completion(log.discard)


async def handle_log_stop(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    log = conn.log_slot.detach()
    if log is None:
        return _not_logging(conn)
    log.event(CLOSE_EVENT)
    await run_to_completion(log.close)  # detached above, so it must be closed now
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
        dropped_entries=status["dropped_entries"],
        dropped_events=status["dropped_events"],
        admission_error=status["admission_error"],
    )


async def handle_log_status(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    log = conn.log_slot.log
    if log is None:
        return _ok(message=f"{conn.port} is not logging.", connection_id=conn.connection_id, logging=False)
    status = log.status()
    message = (
        f"{conn.port} has a log with stopped admission."
        if status["admission_error"]
        else f"{conn.port} is logging."
    )
    return _ok(message=message, connection_id=conn.connection_id, logging=True, **status)


async def handle_log_mark(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    label = args["label"]
    if not isinstance(label, str):
        return _err("invalid_params", "label must be a string.")
    log = conn.log_slot.log
    if log is None:
        return _not_logging(conn)
    if not log.event(MARK_EVENT, label=label):
        reason = log.status()["admission_error"] or "Metadata was unsupported or exceeded an admission limit."
        return _err("log_failed", f"Marker was not recorded. {reason}")
    return _ok(message=f"Marked {conn.port}.", connection_id=conn.connection_id, label=label)


HANDLERS: dict[str, Any] = {
    "serial.log.start": handle_log_start,
    "serial.log.stop": handle_log_stop,
    "serial.log.status": handle_log_status,
    "serial.log.mark": handle_log_mark,
}
