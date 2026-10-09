"""Control line tools: set and pulse DTR and RTS."""

from __future__ import annotations

import time
from typing import Any

import anyio
from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.responses import (
    _coerce_bool,
    _ok,
    script_running_error,
)
from serial_mcp_server.mcp.state import SerialState

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    # ---- set_dtr ----
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
    # ---- set_rts ----
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
    # ---- pulse_dtr ----
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
    # ---- pulse_rts ----
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
    await anyio.sleep(duration_ms / 1000.0)
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
    await anyio.sleep(duration_ms / 1000.0)
    # No claim check: the pulse began before any claim; a line left low under a script is worse than one edge.
    conn.ser.rts = True
    conn.log_slot.event("rts", value=True)
    conn.last_seen_ts = time.time()
    return _ok(message=f"Pulsed RTS low for {duration_ms}ms on {conn.port}.", duration_ms=duration_ms)


# ---------------------------------------------------------------------------
# Handler map
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Any] = {
    "serial.set_dtr": handle_set_dtr,
    "serial.set_rts": handle_set_rts,
    "serial.pulse_dtr": handle_pulse_dtr,
    "serial.pulse_rts": handle_pulse_rts,
}
