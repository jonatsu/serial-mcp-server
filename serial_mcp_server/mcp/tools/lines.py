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
from serial_mcp_server.mcp.tools.schemas import CONNECTION_ID

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    # ---- set_dtr ----
    Tool(
        name="serial.set_dtr",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
        description=(
            "Use when the protocol spec or the user says to hold the DTR (Data Terminal Ready) line high or low, "
            "for example to keep a board in reset or select a boot mode. The effect is device-specific."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "value": {
                    "type": ["boolean", "string"],
                    "description": "true sets the line high, false sets it low.",
                },
            },
            "required": ["connection_id", "value"],
        },
    ),
    # ---- set_rts ----
    Tool(
        name="serial.set_rts",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
        description=(
            "Use when the protocol spec or the user says to hold the RTS (Request To Send) line high or low, "
            "for example to select a boot mode. The effect is device-specific."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "value": {
                    "type": ["boolean", "string"],
                    "description": "true sets the line high, false sets it low.",
                },
            },
            "required": ["connection_id", "value"],
        },
    ),
    # ---- pulse_dtr ----
    Tool(
        name="serial.pulse_dtr",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use to reset a board that resets on DTR, such as many Arduino and ESP32 boards, when the protocol "
            "spec or the user confirms it: sets DTR low, waits duration_ms, then sets it high. The effect is "
            "device-specific, so read the boot output afterwards."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
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
            "Use when the protocol spec or the user says a device resets or enters its bootloader on RTS: sets "
            "RTS low, waits duration_ms, then sets it high. The effect is device-specific."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
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


# Longest pulse, in milliseconds. A larger duration_ms is clamped, not rejected; the reply reports the value
# used.
_MAX_PULSE_MS = 10_000


async def handle_pulse_dtr(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    """Pulse DTR low for ``duration_ms`` (100 ms by default, at most ``_MAX_PULSE_MS``), then set it high.

    The wait is an event-loop sleep, so other tools run meanwhile. A request cancelled during the wait skips
    the final step and leaves the line low. ``handle_pulse_rts`` behaves the same way.
    """
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
