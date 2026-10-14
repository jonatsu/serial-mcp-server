"""Trace tool definitions and handlers — status, tail."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.responses import _ok
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.trace import get_trace_buffer

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="serial.trace.status",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use to check whether tool-call tracing is on before relying on it: returns the tracing settings, "
            "the trace file, its file_state (disabled, active, failed, or closed), sanitized file_error "
            "(exception class and errno), and the number of recorded events. A failed file sink leaves memory "
            "tracing and tool results available."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="serial.trace.tail",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use to review what happened in this session, for example to debug a failed sequence or to summarize "
            "the steps for the user: returns the last n trace events, a start event with the arguments and an end "
            "event with ok, the error code, and the duration for each tool call. "
            "Written data, script sources, and script variables appear only when the operator turns on "
            "payload tracing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "n": {
                    "type": ["integer", "string"],
                    "description": "Number of recent events to return (default 50).",
                    "default": 50,
                },
            },
            "required": [],
        },
    ),
]

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_trace_status(_state: SerialState, _args: dict[str, Any]) -> dict[str, Any]:
    buf = get_trace_buffer()
    if buf is None:
        return _ok(enabled=False, file_state="disabled", file_error=None)
    return _ok(**buf.status())


async def handle_trace_tail(_state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    buf = get_trace_buffer()
    if buf is None:
        return _ok(enabled=False)
    n = int(args.get("n", 50))
    return _ok(events=buf.tail(n))


HANDLERS: dict[str, Any] = {
    "serial.trace.status": handle_trace_status,
    "serial.trace.tail": handle_trace_tail,
}
