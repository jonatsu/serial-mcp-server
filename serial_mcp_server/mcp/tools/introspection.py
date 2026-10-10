"""Introspection tool definitions and handlers — list connections."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.responses import _ok
from serial_mcp_server.mcp.state import SerialState

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="serial.connections.list",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use when you have lost track of a connection_id, or before opening a port that may already be "
            "open: lists every open connection with its port, baud rate, encoding, buffered bytes, mirror, and "
            "timestamps. serial.connection_status gives one connection's full configuration."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
]

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_connections_list(state: SerialState, _args: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for conn in state.connections.values():
        entry: dict[str, Any] = {
            "connection_id": conn.connection_id,
            "port": conn.port,
            "is_open": conn.ser.is_open,
            "baudrate": conn.baudrate,
            "encoding": conn.encoding,
            "opened_at": conn.opened_at,
            "last_seen_ts": conn.last_seen_ts,
            "buffered_bytes": conn.buffer.available,
        }
        if conn.reader is not None:
            mirror = conn.reader.mirror_info()
            if mirror is not None:
                entry["mirror"] = mirror
        items.append(entry)
    return _ok(
        message=f"{len(items)} connection(s).",
        connections=items,
        count=len(items),
    )


HANDLERS: dict[str, Any] = {
    "serial.connections.list": handle_connections_list,
}
