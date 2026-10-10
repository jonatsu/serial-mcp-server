"""Tests for serial_mcp_server.mcp.writes — running one write job for a tool call."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import anyio
import pytest
import serial as pyserial

from serial_mcp_server.mcp.writes import duration_error, run_write_job
from serial_mcp_server.port.pacing import NO_PACING, Pacing
from serial_mcp_server.port.writer import SerialWriter
from tests.helpers import skipped_timed_waits, start_request

CHUNK_PACING = Pacing(chunk_size=1, chunk_delay_ms=1)


@pytest.fixture
def port():
    ser = MagicMock()
    ser.is_open = True
    ser.write.side_effect = len
    return ser


@pytest.fixture
def conn(port):
    writer = SerialWriter(port)
    writer.start()
    yield SimpleNamespace(writer=writer)
    writer.stop()
    writer.join()


class Recorder:
    """An on_done callback that records each call."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.called = threading.Event()

    def __call__(self, bytes_written: int) -> None:
        self.calls.append(bytes_written)
        self.called.set()


def blocking_port(port) -> threading.Event:
    """Make the port's first write wait until the returned event is set, so later jobs stay queued."""
    release = threading.Event()
    first = threading.Event()

    def write(data: bytes) -> int:
        if not first.is_set():
            first.set()
            release.wait(5)
        return len(data)

    port.write.side_effect = write
    port.first_write_started = first
    return release


async def test_a_write_returns_its_byte_count_and_reports_it_once(conn, port):
    done = Recorder()

    written, error = await run_write_job(conn, b"hello", NO_PACING, on_done=done)

    assert (written, error) == (5, None)
    assert done.called.wait(1)
    assert done.calls == [5]
    port.write.assert_called_once_with(b"hello")


async def test_a_closed_writer_returns_connection_closed_without_writing(conn, port):
    conn.writer.stop()
    done = Recorder()

    written, error = await run_write_job(conn, b"hello", NO_PACING, on_done=done)

    assert written == 0
    assert error["error"]["code"] == "connection_closed"
    assert done.calls == [0]
    port.write.assert_not_called()


async def test_a_write_queued_when_the_connection_closes_returns_connection_closed(conn, port):
    release = blocking_port(port)
    first = conn.writer.submit(b"first", NO_PACING)
    assert port.first_write_started.wait(1)

    async with anyio.create_task_group() as tg:
        outcome = {}

        async def queued() -> None:
            outcome["result"] = await run_write_job(conn, b"second", NO_PACING)

        tg.start_soon(queued)
        await anyio.wait_all_tasks_blocked()
        conn.writer.stop()
        release.set()

    written, error = outcome["result"]
    assert written == 0
    assert error["error"]["code"] == "connection_closed"
    assert first.future.result(timeout=1) == 5


async def test_a_write_that_times_out_partway_returns_write_aborted_with_the_bytes_sent(conn, port):
    port.write.side_effect = [1, 1, pyserial.SerialTimeoutException("stuck")]
    done = Recorder()

    with skipped_timed_waits():
        written, error = await run_write_job(conn, b"abcd", CHUNK_PACING, on_done=done)

    assert written == 2
    assert error["error"]["code"] == "write_aborted"
    assert error["bytes_written"] == 2
    assert done.called.wait(1)
    assert done.calls == [2]


async def test_a_request_cancelled_while_its_write_is_queued_drops_the_write(conn, port):
    # Cancelling the future and setting the job's cancel event each keep a queued write off the wire on their own,
    # so this fails only when neither happens.
    release = blocking_port(port)
    conn.writer.submit(b"first", NO_PACING)
    assert port.first_write_started.wait(1)
    done = Recorder()

    async with anyio.create_task_group() as tg:
        request = start_request(tg, run_write_job, conn, b"queued", NO_PACING, done)
        await anyio.wait_all_tasks_blocked()
        request.cancel()
    release.set()

    assert request.cancelled
    assert done.called.wait(1)
    assert done.calls == [0]
    conn.writer.stop()
    conn.writer.join()
    assert [call.args[0] for call in port.write.call_args_list] == [b"first"]


async def test_a_request_cancelled_during_a_paced_write_stops_it_between_segments(conn, port):
    second_segment = threading.Event()
    stop_here = threading.Event()

    def write(data: bytes) -> int:
        if port.write.call_count == 2:
            second_segment.set()
            stop_here.wait(5)
        return len(data)

    port.write.side_effect = write
    done = Recorder()

    async with anyio.create_task_group() as tg:
        request = start_request(tg, run_write_job, conn, b"abcdefgh", CHUNK_PACING, done)
        await anyio.to_thread.run_sync(second_segment.wait, 1)
        request.cancel()
        await anyio.wait_all_tasks_blocked()
        stop_here.set()

    assert request.cancelled
    assert done.called.wait(1)
    assert done.calls == [2]  # the segment in progress finishes; nothing after it is sent
    assert port.write.call_count == 2


class TestDurationError:
    def test_unpaced_and_short_writes_pass(self):
        assert duration_error(0, 60) is None
        assert duration_error(60, 60) is None

    def test_a_write_over_the_cap_names_the_variable(self):
        error = duration_error(61.5, 60)

        assert error["error"]["code"] == "invalid_params"
        assert "SERIAL_MCP_MAX_WRITE_SECONDS" in error["error"]["message"]
