"""Tests for the input and output tools: read, readline, read_until, write, and flush."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import anyio
import anyio.to_thread
import pytest
import serial as pyserial

from serial_mcp_server import config
from serial_mcp_server.config import ConfigError
from serial_mcp_server.mcp.tools.connection import handle_close
from serial_mcp_server.mcp.tools.io import (
    HANDLERS,
    TOOLS,
    _format_data,
    handle_flush,
    handle_read,
    handle_read_until,
    handle_readline,
    handle_write,
)
from serial_mcp_server.port.pacing import Pacing
from serial_mcp_server.port.writer import SerialWriter
from tests.helpers import reconfigure, skipped_timed_waits, start_request, writer_goes_idle


class TestToolRegistration:
    def test_every_tool_has_a_handler(self):
        assert {t.name for t in TOOLS} == set(HANDLERS)

    def test_tools_in_registration_order(self):
        assert [t.name for t in TOOLS] == [
            "serial.read",
            "serial.write",
            "serial.readline",
            "serial.read_until",
            "serial.flush",
        ]


class TestRead:
    async def test_read_text(self, connected_entry):
        state, conn = connected_entry
        conn.buffer.write(b"hello")
        result = await handle_read(state, {"connection_id": "s1"})
        assert result["ok"]
        assert result["data"] == "hello"
        assert result["n_read"] == 5
        assert result["format"] == "text"

    async def test_read_hex(self, connected_entry):
        state, conn = connected_entry
        conn.buffer.write(b"\x01\x02\x03")
        result = await handle_read(state, {"connection_id": "s1", "as": "hex"})
        assert result["ok"]
        assert result["data"] == "010203"

    async def test_read_base64(self, connected_entry):
        state, conn = connected_entry
        conn.buffer.write(b"\x01\x02\x03")
        result = await handle_read(state, {"connection_id": "s1", "as": "base64"})
        assert result["ok"]
        assert result["data"] == "AQID"

    async def test_read_empty_on_timeout(self, connected_entry):
        state, conn = connected_entry
        # Buffer is empty, should return empty after timeout
        result = await handle_read(state, {"connection_id": "s1", "timeout_ms": 10})
        assert result["ok"]
        assert result["n_read"] == 0


class TestWrite:
    async def test_write_text(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.return_value = 5
        result = await handle_write(state, {"connection_id": "s1", "data": "hello"})
        assert result["ok"]
        assert result["bytes_written"] == 5
        conn.ser.write.assert_called_once_with(b"hello")

    async def test_write_hex(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.return_value = 3
        result = await handle_write(state, {"connection_id": "s1", "data": "010203", "as": "hex"})
        assert result["ok"]
        conn.ser.write.assert_called_once_with(b"\x01\x02\x03")

    async def test_write_invalid_hex(self, connected_entry):
        state, conn = connected_entry
        result = await handle_write(state, {"connection_id": "s1", "data": "ZZZZ", "as": "hex"})
        assert not result["ok"]
        assert result["error"]["code"] == "invalid_value"

    async def test_write_append_newline(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.return_value = 6
        result = await handle_write(state, {"connection_id": "s1", "data": "hello", "append_newline": True})
        assert result["ok"]
        conn.ser.write.assert_called_once_with(b"hello\n")

    async def test_write_with_connection_character_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=8)
        conn.ser.write.side_effect = len
        with skipped_timed_waits() as sleeps:
            result = await handle_write(state, {"connection_id": "s1", "data": "ABC"})

        assert result["bytes_written"] == 3
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"A", b"B", b"C"]
        assert sleeps == [0.008, 0.008]

    async def test_write_with_connection_chunk_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(chunk_size=2, chunk_delay_ms=5)
        conn.ser.write.side_effect = len
        with skipped_timed_waits() as sleeps:
            result = await handle_write(state, {"connection_id": "s1", "data": "ABCDE"})

        assert result["bytes_written"] == 5
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"AB", b"CD", b"E"]
        assert sleeps == [0.005, 0.005]

    async def test_write_with_per_call_character_delay_override(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=2)
        conn.ser.write.side_effect = len
        with skipped_timed_waits() as sleeps:
            result = await handle_write(
                state,
                {"connection_id": "s1", "data": "ABC", "inter_char_delay_ms": 8},
            )

        assert result["bytes_written"] == 3
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"A", b"B", b"C"]
        assert sleeps == [0.008, 0.008]

    async def test_write_with_per_call_chunk_override_from_character_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=8)
        conn.ser.write.side_effect = len
        with skipped_timed_waits() as sleeps:
            result = await handle_write(
                state,
                {
                    "connection_id": "s1",
                    "data": "ABCDEFG",
                    "chunk_size": 3,
                    "chunk_delay_ms": 5,
                },
            )

        assert result["bytes_written"] == 7
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"ABC", b"DEF", b"G"]
        assert sleeps == [0.005, 0.005]

    async def test_write_zero_override_disables_connection_chunk_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(chunk_size=2, chunk_delay_ms=5)
        conn.ser.write.return_value = 5
        with skipped_timed_waits() as sleeps:
            result = await handle_write(
                state,
                {"connection_id": "s1", "data": "ABCDE", "inter_char_delay_ms": 0},
            )

        assert result["bytes_written"] == 5
        conn.ser.write.assert_called_once_with(b"ABCDE")
        assert sleeps == []

    async def test_write_zero_character_delay_disables_connection_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=8)
        conn.ser.write.return_value = 5
        with skipped_timed_waits() as sleeps:
            result = await handle_write(
                state,
                {"connection_id": "s1", "data": "ABCDE", "inter_char_delay_ms": 0},
            )

        assert result["bytes_written"] == 5
        conn.ser.write.assert_called_once_with(b"ABCDE")
        assert sleeps == []

    @pytest.mark.parametrize(
        "delay",
        [
            -1,
            1001,
            "not-a-number",
            True,
            1.5,
        ],
    )
    async def test_write_rejects_invalid_character_delay_override(self, connected_entry, delay):
        state, conn = connected_entry
        result = await handle_write(
            state,
            {"connection_id": "s1", "data": "ABCDE", "inter_char_delay_ms": delay},
        )
        assert not result["ok"]
        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()

    @pytest.mark.parametrize(
        "pacing",
        [
            {"chunk_size": 3},
            {"chunk_delay_ms": 5},
            {"chunk_size": 0, "chunk_delay_ms": 5},
            {"chunk_size": 3, "chunk_delay_ms": 0},
            {"chunk_size": -1, "chunk_delay_ms": 5},
            {"chunk_size": 3, "chunk_delay_ms": -1},
            {"chunk_size": 3, "chunk_delay_ms": 1001},
            {"inter_char_delay_ms": 8, "chunk_size": 3, "chunk_delay_ms": 5},
        ],
    )
    async def test_write_rejects_invalid_chunk_pacing(self, connected_entry, pacing):
        state, conn = connected_entry
        result = await handle_write(state, {"connection_id": "s1", "data": "ABCDE", **pacing})
        assert not result["ok"]
        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()


class TestReadline:
    async def test_readline(self, connected_entry):
        state, conn = connected_entry
        conn.buffer.write(b"hello\n")
        result = await handle_readline(state, {"connection_id": "s1"})
        assert result["ok"]
        assert result["data"] == "hello\n"

    async def test_readline_custom_newline(self, connected_entry):
        state, conn = connected_entry
        conn.buffer.write(b"hello\r\n")
        result = await handle_readline(state, {"connection_id": "s1", "newline": "\r\n"})
        assert result["ok"]
        assert result["data"] == "hello\r\n"


class TestReadUntil:
    async def test_read_until_custom_delimiter(self, connected_entry):
        state, conn = connected_entry
        conn.buffer.write(b"hello>")
        result = await handle_read_until(state, {"connection_id": "s1", "delimiter": ">"})
        assert result["ok"]
        assert result["data"] == "hello>"


class TestFlush:
    async def test_flush_both(self, connected_entry):
        state, conn = connected_entry
        result = await handle_flush(state, {"connection_id": "s1"})
        assert result["ok"]
        conn.ser.reset_input_buffer.assert_called_once()
        conn.ser.reset_output_buffer.assert_called_once()

    async def test_flush_input_only(self, connected_entry):
        state, conn = connected_entry
        result = await handle_flush(state, {"connection_id": "s1", "what": "input"})
        assert result["ok"]
        conn.ser.reset_input_buffer.assert_called_once()
        conn.ser.reset_output_buffer.assert_not_called()


class TestClaimRecheckedAfterAwait:
    """A script that claims the connection while a tool awaits must stop that tool's later effects."""

    async def test_flush_does_not_discard_the_scripts_writes(self, connected_entry):
        state, conn = connected_entry
        reading = threading.Event()
        proceed = threading.Event()

        def gated_reset() -> None:
            reading.set()
            assert proceed.wait(5), "the test never released the flush"

        conn.ser.reset_input_buffer.side_effect = gated_reset
        with patch.object(conn.writer, "discard_pending") as discard:
            async with anyio.create_task_group() as tg:
                flush = start_request(tg, handle_flush, state, {"connection_id": "s1"})
                assert await anyio.to_thread.run_sync(reading.wait, 5)
                run, _owner = state.script_jobs.claim("s1", MagicMock())
                assert run is not None
                proceed.set()
                result = await flush.outcome()

        assert result["error"]["code"] == "script_running"
        discard.assert_not_called()


class TestFormatData:
    def test_text(self):
        r = _format_data(b"hello", "text", "utf-8")
        assert r["data"] == "hello"
        assert r["format"] == "text"

    def test_hex(self):
        r = _format_data(b"\xab\xcd", "hex", "utf-8")
        assert r["data"] == "abcd"

    def test_base64(self):
        r = _format_data(b"\x01\x02", "base64", "utf-8")
        assert r["data"] == "AQI="

    def test_text_replace_errors(self):
        r = _format_data(b"\xff\xfe", "text", "utf-8")
        assert "\ufffd" in r["data"]


class _HeldPort:
    """Port double whose first ``write`` blocks until released."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True
        self.is_open = True

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self._first:
            self._first = False
            self.entered.set()
            self.release.wait(2)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


@pytest.fixture()
def held_entry(connected_entry):
    """A connection whose writer drives a ``_HeldPort``."""
    state, conn = connected_entry
    port = _HeldPort()
    conn.writer.stop()
    conn.writer.join()
    conn.ser = port
    conn.writer = SerialWriter(port)
    conn.writer.start()
    yield state, conn, port
    port.release.set()
    conn.writer.stop()
    conn.writer.join()


async def _wait_until_held(port: _HeldPort) -> None:
    assert await anyio.to_thread.run_sync(port.entered.wait, 2)


class TestWriterIntegration:
    async def test_concurrent_writes_do_not_interleave(self, held_entry):
        state, conn, port = held_entry
        args = {"connection_id": "s1", "chunk_size": 2, "chunk_delay_ms": 1}

        async with anyio.create_task_group() as tg:
            first = start_request(tg, handle_write, state, {**args, "data": "AAAA"})
            await _wait_until_held(port)
            second = start_request(tg, handle_write, state, {**args, "data": "BBBB"})
            await anyio.wait_all_tasks_blocked()
            port.release.set()
            results = [await first.outcome(), await second.outcome()]

        assert [r["bytes_written"] for r in results] == [4, 4]
        assert port.writes == [b"AA", b"AA", b"BB", b"BB"]

    async def test_cancelling_a_queued_write_sends_nothing(self, held_entry):
        state, conn, port = held_entry

        async with anyio.create_task_group() as tg:
            first = start_request(tg, handle_write, state, {"connection_id": "s1", "data": "first"})
            await _wait_until_held(port)
            queued = start_request(tg, handle_write, state, {"connection_id": "s1", "data": "never"})
            await anyio.wait_all_tasks_blocked()
            queued.cancel()
            await queued.outcome()
            port.release.set()
            await first.outcome()

        assert queued.cancelled

        assert port.writes == [b"first"]

    async def test_cancelling_a_running_paced_write_stops_it_between_segments(self, held_entry):
        state, conn, port = held_entry
        # Six segments a second apart: left running, the write would take five more seconds.
        args = {"connection_id": "s1", "data": "ABCDEF", "chunk_size": 1, "chunk_delay_ms": 1000}

        async with anyio.create_task_group() as tg:
            write = start_request(tg, handle_write, state, args)
            await _wait_until_held(port)
            write.cancel()
            port.release.set()
            await write.outcome()
        assert write.cancelled

        # Only the cancellation reaches the job here; nothing stops the writer. Within half the
        # delay between segments the job must have ended with its first segment.
        assert await anyio.to_thread.run_sync(writer_goes_idle, conn.writer, "mcp", 0.5)
        assert port.writes == [b"A"]

    async def test_close_fails_a_queued_write_and_ends_the_writer_thread(self, held_entry):
        state, conn, port = held_entry

        async with anyio.create_task_group() as tg:
            first = start_request(tg, handle_write, state, {"connection_id": "s1", "data": "first"})
            await _wait_until_held(port)
            queued = start_request(tg, handle_write, state, {"connection_id": "s1", "data": "queued"})
            await anyio.wait_all_tasks_blocked()
            stopped = threading.Event()
            real_stop = conn.writer.stop
            conn.writer.stop = lambda: (real_stop(), stopped.set())
            closing = start_request(tg, handle_close, state, {"connection_id": "s1"})
            assert await anyio.to_thread.run_sync(stopped.wait, 2)
            port.release.set()
            await closing.outcome()
            queued_result = await queued.outcome()
            await first.outcome()

        assert queued_result["error"]["code"] == "connection_closed"
        assert port.writes == [b"first"]
        assert not conn.writer.alive

    async def test_write_after_the_writer_stops_reports_a_closed_connection(self, held_entry):
        state, conn, _port = held_entry
        conn.writer.stop()

        result = await handle_write(state, {"connection_id": "s1", "data": "x"})

        assert result["error"]["code"] == "connection_closed"

    async def test_flush_output_discards_queued_writes(self, held_entry):
        state, conn, port = held_entry

        async with anyio.create_task_group() as tg:
            first = start_request(tg, handle_write, state, {"connection_id": "s1", "data": "first"})
            await _wait_until_held(port)
            queued = start_request(tg, handle_write, state, {"connection_id": "s1", "data": "queued"})
            await anyio.wait_all_tasks_blocked()
            await handle_flush(state, {"connection_id": "s1", "what": "output"})
            port.release.set()
            await first.outcome()
            queued_result = await queued.outcome()

        assert queued_result["error"]["code"] == "write_aborted"
        assert queued_result["bytes_written"] == 0
        assert port.writes == [b"first"]


class TestWriteDurationCap:
    async def test_write_over_the_cap_is_rejected_before_any_byte_is_sent(self, connected_entry):
        state, conn = connected_entry
        reconfigure(state, max_write_seconds=1.0)

        result = await handle_write(
            state, {"connection_id": "s1", "data": "A" * 200, "inter_char_delay_ms": 8}
        )

        assert result["error"]["code"] == "invalid_params"
        assert "1.6 s" in result["error"]["message"]
        assert "1 s cap" in result["error"]["message"]
        conn.ser.write.assert_not_called()

    async def test_write_at_the_cap_is_accepted(self, connected_entry):
        state, conn = connected_entry
        reconfigure(state, max_write_seconds=0.016)
        conn.ser.write.side_effect = len

        with skipped_timed_waits():
            result = await handle_write(
                state, {"connection_id": "s1", "data": "AB", "inter_char_delay_ms": 8}
            )

        assert result["bytes_written"] == 2

    async def test_default_cap_allows_7500_characters_at_8_ms(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.side_effect = len

        with skipped_timed_waits():
            result = await handle_write(
                state, {"connection_id": "s1", "data": "A" * 7500, "inter_char_delay_ms": 8}
            )

        assert result["bytes_written"] == 7500

    @pytest.mark.parametrize("raw", ["abc", "0", "-5", "nan", "inf"])
    async def test_an_invalid_cap_stops_the_start(self, raw):
        with pytest.raises(ConfigError, match="SERIAL_MCP_MAX_WRITE_SECONDS"):
            config.load({"SERIAL_MCP_MAX_WRITE_SECONDS": raw})

    async def test_over_the_default_cap_is_rejected(self, connected_entry):
        state, conn = connected_entry

        result = await handle_write(
            state, {"connection_id": "s1", "data": "A" * 7501, "inter_char_delay_ms": 8}
        )

        assert result["error"]["code"] == "invalid_params"
        assert "60 s cap" in result["error"]["message"]

    async def test_timeout_mid_write_reports_bytes_written(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.side_effect = [2, 2, pyserial.SerialTimeoutException("write timeout")]

        with skipped_timed_waits():
            result = await handle_write(
                state,
                {"connection_id": "s1", "data": "ABCDEFGH", "chunk_size": 2, "chunk_delay_ms": 1},
            )

        assert result["error"]["code"] == "write_aborted"
        assert result["bytes_written"] == 4

    async def test_unpaced_write_ignores_the_cap(self, connected_entry):
        state, conn = connected_entry
        reconfigure(state, max_write_seconds=0.001)
        conn.ser.write.return_value = 3

        result = await handle_write(state, {"connection_id": "s1", "data": "abc"})

        assert result["bytes_written"] == 3


class TestExposedToolText:
    """The text MCP clients show the agent must follow the behavior; see CONTRIBUTING.md."""

    @staticmethod
    def description(name: str) -> str:
        return next(tool.description or "" for tool in TOOLS if tool.name == name)

    def test_write_names_the_cap_and_the_early_stop_error(self):
        text = self.description("serial.write")
        assert "SERIAL_MCP_MAX_WRITE_SECONDS" in text
        assert "write_aborted" in text

    def test_flush_says_it_cancels_queued_writes(self):
        assert "cancels writes still queued" in self.description("serial.flush")
