"""File input tool — serial.send_file sends a prepared file to the device."""

from __future__ import annotations

import time
from typing import Any

import anyio
import anyio.to_thread
from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.confined_files import MAX_CONFINED_FILE_BYTES, read_confined_file
from serial_mcp_server.mcp.responses import _err, _ok, script_running_error
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.tools.io import parse_write_pacing
from serial_mcp_server.mcp.writes import duration_error, run_write_job
from serial_mcp_server.port.pacing import Pacing, wire_seconds
from serial_mcp_server.port.writer import UNPACED_SEGMENT_BYTES
from serial_mcp_server.portlog.formats import SEND_FILE_END_EVENT, SEND_FILE_START_EVENT

MAX_SEND_FILE_BYTES = MAX_CONFINED_FILE_BYTES

TOOLS: list[Tool] = [
    Tool(
        name="serial.send_file",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use for long inputs, such as a U-Boot environment or a shell script, that would otherwise go "
            "in serial.write arguments. Sends the file at path with the connection's pacing; pacing arguments "
            "override it, as on serial.write. Accepts regular files of at most 1 MiB inside the operator's "
            "files directory (SERIAL_MCP_FILES_DIR). Returns bytes_written. "
            "An estimated time over the server cap, or a transfer that stops early, returns the same errors "
            "as serial.write."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "path": {"type": "string", "description": "File path relative to the files directory."},
                "inter_char_delay_ms": {
                    "type": ["integer", "string"],
                    "description": "Override the delay between characters in milliseconds (0 disables; max 1000).",
                },
                "chunk_size": {
                    "type": ["integer", "string"],
                    "description": "Override bytes per chunk; set with chunk_delay_ms.",
                },
                "chunk_delay_ms": {
                    "type": ["integer", "string"],
                    "description": "Override the delay after each chunk in milliseconds (0 disables; max 1000).",
                },
            },
            "required": ["connection_id", "path"],
        },
    ),
]


def _estimated_seconds(pacing: Pacing, size: int, baudrate: int) -> float:
    """The larger of a transfer's pauses between segments and its time on the wire."""
    return max(pacing.estimated_seconds(size), wire_seconds(size, baudrate))


async def handle_send_file(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    conn = state.get_connection(args["connection_id"])
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    requested = args["path"]
    root = state.settings.files_dir
    try:
        pacing = parse_write_pacing(args, conn.writer.pacing)
        resolved, payload = await anyio.to_thread.run_sync(
            read_confined_file, root, requested, MAX_SEND_FILE_BYTES, abandon_on_cancel=True
        )
    except ValueError as exc:  # includes PathRefused
        return _err("invalid_params", str(exc))
    except OSError as exc:
        return _err("file_unreadable", f"Could not read {requested!r}: {exc.strerror or exc}")

    too_long = duration_error(
        _estimated_seconds(pacing, len(payload), conn.baudrate), state.settings.max_write_seconds
    )
    if too_long is not None:
        return too_long
    if not pacing.is_paced:
        pacing = Pacing(chunk_size=UNPACED_SEGMENT_BYTES)

    # Report the path inside the files directory, not the operator's absolute layout.
    shown_path = resolved.relative_to(root.resolve()).as_posix()
    # A script may have claimed the connection while the file was read; nothing awaits from here to the submit.
    blocked = script_running_error(state, conn.connection_id)
    if blocked is not None:
        return blocked
    conn.log_slot.event(SEND_FILE_START_EVENT, path=shown_path, size=len(payload))
    written, error = await run_write_job(
        conn,
        payload,
        pacing,
        on_done=lambda count: conn.log_slot.event(SEND_FILE_END_EVENT, path=shown_path, bytes_written=count),
    )
    if error is not None:
        return error

    conn.last_seen_ts = time.time()
    return _ok(
        message=f"Sent {written} byte(s) from {shown_path} to {conn.port}.",
        bytes_written=written,
        path=shown_path,
    )


HANDLERS: dict[str, Any] = {"serial.send_file": handle_send_file}
