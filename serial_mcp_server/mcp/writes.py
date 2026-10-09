"""Running a write job for a tool: the duration cap, and waiting for the writer without blocking the loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from serial_mcp_server.mcp.responses import _err
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.pacing import Pacing
from serial_mcp_server.port.writer import WriteAborted, WriterClosed


def duration_error(estimate_seconds: float, cap: float) -> dict[str, Any] | None:
    """Return an error when an estimated transfer time exceeds the server-wide *cap*, in seconds."""
    if estimate_seconds <= 0:
        return None
    if estimate_seconds <= cap:
        return None
    return _err(
        "invalid_params",
        f"Estimated write time {estimate_seconds:.1f} s exceeds the {cap:g} s cap "
        "(SERIAL_MCP_MAX_WRITE_SECONDS). Send less data per call or reduce the pacing delay.",
    )


def _bytes_written_by(future: Future[int]) -> int:
    if future.cancelled():
        return 0
    error = future.exception()
    if isinstance(error, WriteAborted):
        return error.bytes_written
    return 0 if error is not None else future.result()


async def run_write_job(
    conn: SerialConnection,
    payload: bytes,
    pacing: Pacing,
    on_done: Callable[[int], None] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Queue *payload* on the connection's writer and wait for it.

    *on_done* is called exactly once with the bytes written, after the writer has stopped
    touching the port, so anything it logs follows the write's own port-log entries. It runs
    on the writer's thread, or on the caller's when the write never started.

    Returns:
        The bytes written and an error response, which is None on success. A cancelled request
        stops the write between segments and re-raises.
    """
    try:
        job = conn.writer.submit(payload, pacing)
    except WriterClosed as exc:
        if on_done is not None:
            on_done(0)
        return 0, _err("connection_closed", str(exc))
    if on_done is not None:
        job.future.add_done_callback(lambda future: on_done(_bytes_written_by(future)))
    try:
        return await asyncio.wrap_future(job.future), None
    except asyncio.CancelledError:
        job.cancel.set()
        raise
    except WriterClosed as exc:
        return 0, _err("connection_closed", str(exc))
    except WriteAborted as exc:
        return exc.bytes_written, {**_err("write_aborted", str(exc)), "bytes_written": exc.bytes_written}
