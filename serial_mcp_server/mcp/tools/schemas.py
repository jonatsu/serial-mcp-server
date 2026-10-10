"""Property schemas that several tools share, so each fact the agent reads has one wording."""

from __future__ import annotations

from typing import Any

from serial_mcp_server.port.pacing import MAX_PACING_DELAY_MS

CONNECTION_ID: dict[str, Any] = {
    "type": "string",
    "description": "The connection_id that serial.open returned; serial.connections.list lists the open ones.",
}

READ_TIMEOUT_MS: dict[str, Any] = {
    "type": ["integer", "string"],
    "description": "Wait at most this many milliseconds for this call only; default the connection's read timeout.",
}

DATA_FORMAT: dict[str, Any] = {
    "type": "string",
    "enum": ["text", "hex", "base64"],
    "default": "text",
    "description": "text decodes with the connection's encoding; use hex or base64 for binary data.",
}


def max_bytes(default: int) -> dict[str, Any]:
    return {
        "type": ["integer", "string"],
        "default": default,
        "description": f"Return at most this many bytes (default {default}); the rest stays buffered.",
    }


def pacing_properties(*, per_call: bool) -> dict[str, dict[str, Any]]:
    """The three write-pacing properties, worded for a connection setting or a one-call override."""
    scope = (
        "For this call only, replacing the connection's pacing. "
        if per_call
        else "Applies to every later write on the connection and to read/write mirror input. "
    )
    return {
        "inter_char_delay_ms": {
            "type": ["integer", "string"],
            "description": (
                f"{scope}Pause after each character, in milliseconds (0 to {MAX_PACING_DELAY_MS}), "
                "for a device that drops fast input. 0 turns it off. Cannot be combined with chunk pacing."
            ),
        },
        "chunk_size": {
            "type": ["integer", "string"],
            "description": "Bytes per chunk for chunk pacing; set together with chunk_delay_ms. 0 turns it off.",
        },
        "chunk_delay_ms": {
            "type": ["integer", "string"],
            "description": f"Pause after each chunk drains, in milliseconds (0 to {MAX_PACING_DELAY_MS}).",
        },
    }
