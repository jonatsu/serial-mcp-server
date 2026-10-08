"""File input tool — serial.send_file sends a prepared file to the device."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.handlers_serial import (
    MAX_READ_BYTES,
    duration_error,
    parse_write_pacing,
    run_write_job,
    wire_seconds,
)
from serial_mcp_server.helpers import _err, _ok
from serial_mcp_server.paths import files_dir, read_confined_file
from serial_mcp_server.state import SerialState
from serial_mcp_server.writer import Pacing

SEND_FILE_START_EVENT = "send_file_start"
SEND_FILE_END_EVENT = "send_file_end"

MAX_SEND_FILE_BYTES = MAX_READ_BYTES  # 1 MiB; the whole file is held in memory

# Without pacing, a file still goes out in segments so cancel and bytes_written work as for paced writes.
UNPACED_SEGMENT_BYTES = 4096

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
    requested = args["path"]
    root = files_dir()
    try:
        pacing = parse_write_pacing(args, conn.writer.pacing)
        resolved, payload = await asyncio.to_thread(read_confined_file, root, requested, MAX_SEND_FILE_BYTES)
    except ValueError as exc:  # includes PathRefused
        return _err("invalid_params", str(exc))
    except OSError as exc:
        return _err("file_unreadable", f"Could not read {requested!r}: {exc.strerror or exc}")

    too_long = duration_error(_estimated_seconds(pacing, len(payload), conn.baudrate))
    if too_long is not None:
        return too_long
    if not pacing.is_paced:
        pacing = Pacing(chunk_size=UNPACED_SEGMENT_BYTES)

    # Report the path inside the files directory, not the operator's absolute layout.
    shown_path = resolved.relative_to(root.resolve()).as_posix()
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
