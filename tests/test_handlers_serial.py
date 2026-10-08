"""Tests for serial tool handlers in serial_mcp_server.handlers_serial."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from serial_mcp_server.handlers_serial import (
    HANDLERS,
    TOOLS,
    _format_data,
    handle_close,
    handle_connection_status,
    handle_flush,
    handle_list_ports,
    handle_open,
    handle_pulse_dtr,
    handle_pulse_rts,
    handle_read,
    handle_read_until,
    handle_readline,
    handle_set_dtr,
    handle_set_rts,
    handle_write,
)
from serial_mcp_server.state import SerialState
from serial_mcp_server.writer import Pacing, SerialWriter


class TestToolRegistration:
    def test_all_tools_have_handlers(self):
        tool_names = {t.name for t in TOOLS}
        handler_names = set(HANDLERS.keys())
        assert tool_names == handler_names

    def test_tool_count(self):
        assert len(TOOLS) == 13


class TestListPorts:
    async def test_list_ports(self):
        state = SerialState()
        mock_port = MagicMock()
        mock_port.device = "/dev/ttyUSB0"
        mock_port.name = "ttyUSB0"
        mock_port.description = "USB Serial"
        mock_port.hwid = "USB VID:PID=1234:5678"
        mock_port.vid = 0x1234
        mock_port.pid = 0x5678
        mock_port.serial_number = "ABC123"
        mock_port.manufacturer = "TestCo"
        mock_port.product = "TestDevice"
        mock_port.location = "1-1"

        with patch(
            "serial_mcp_server.handlers_serial.serial.tools.list_ports.comports", return_value=[mock_port]
        ):
            result = await handle_list_ports(state, {})
        assert result["ok"]
        assert result["count"] == 1
        port = result["ports"][0]
        assert port["device"] == "/dev/ttyUSB0"
        assert port["vid"] == "0x1234"
        assert port["pid"] == "0x5678"

    async def test_list_ports_empty(self):
        state = SerialState()
        with patch("serial_mcp_server.handlers_serial.serial.tools.list_ports.comports", return_value=[]):
            result = await handle_list_ports(state, {})
        assert result["ok"]
        assert result["count"] == 0


class TestOpen:
    async def test_open_success(self):
        state = SerialState()
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_reader = MagicMock()
        mock_reader.mirror_info.return_value = None
        with (
            patch("serial_mcp_server.handlers_serial.pyserial.Serial", return_value=mock_ser),
            patch("serial_mcp_server.handlers_serial.create_reader", return_value=mock_reader),
        ):
            result = await handle_open(state, {"port": "/dev/ttyUSB0"})
        assert result["ok"]
        assert "connection_id" in result
        assert result["config"]["baudrate"] == 115200
        mock_reader.start.assert_called_once()

    async def test_open_with_mirror(self):
        state = SerialState()
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_reader = MagicMock()
        mock_reader.mirror_info.return_value = {
            "pty_path": "/dev/ttys004",
            "link": "/tmp/serial-mcp0",
            "mode": "ro",
        }
        with (
            patch("serial_mcp_server.handlers_serial.pyserial.Serial", return_value=mock_ser),
            patch("serial_mcp_server.handlers_serial.create_reader", return_value=mock_reader),
        ):
            result = await handle_open(state, {"port": "/dev/ttyUSB0"})
        assert result["ok"]
        assert result["mirror"]["pty_path"] == "/dev/ttys004"
        assert result["mirror"]["mode"] == "ro"

    async def test_open_invalid_parity(self):
        state = SerialState()
        result = await handle_open(state, {"port": "/dev/ttyUSB0", "parity": "X"})
        assert not result["ok"]
        assert result["error"]["code"] == "invalid_params"

    async def test_open_invalid_stopbits(self):
        state = SerialState()
        result = await handle_open(state, {"port": "/dev/ttyUSB0", "stopbits": 3})
        assert not result["ok"]
        assert result["error"]["code"] == "invalid_params"

    async def test_open_with_character_pacing(self, serial_state, mock_serial, mock_reader):
        with (
            patch("serial_mcp_server.handlers_serial.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.handlers_serial.create_reader", return_value=mock_reader) as make_reader,
        ):
            result = await handle_open(
                serial_state,
                {"port": "/dev/ttyUSB0", "inter_char_delay_ms": 8},
            )

        assert result["ok"]
        assert result["config"]["inter_char_delay_ms"] == 8
        assert make_reader.call_args.kwargs["writer"].pacing == Pacing(8, 0, 0)

    async def test_open_with_chunk_pacing(self, serial_state, mock_serial, mock_reader):
        with (
            patch("serial_mcp_server.handlers_serial.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.handlers_serial.create_reader", return_value=mock_reader) as make_reader,
        ):
            result = await handle_open(
                serial_state,
                {"port": "/dev/ttyUSB0", "chunk_size": 3, "chunk_delay_ms": 5},
            )

        assert result["ok"]
        assert result["config"]["inter_char_delay_ms"] == 0
        assert result["config"]["chunk_size"] == 3
        assert result["config"]["chunk_delay_ms"] == 5
        assert make_reader.call_args.kwargs["writer"].pacing == Pacing(0, 3, 5)

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
    async def test_open_rejects_invalid_character_delay(self, delay):
        result = await handle_open(SerialState(), {"port": "/dev/ttyUSB0", "inter_char_delay_ms": delay})
        assert not result["ok"]
        assert result["error"]["code"] == "invalid_params"

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
    async def test_open_rejects_invalid_chunk_pacing(self, pacing):
        result = await handle_open(SerialState(), {"port": "/dev/ttyUSB0", **pacing})
        assert not result["ok"]
        assert result["error"]["code"] == "invalid_params"

    async def test_open_duplicate_port(self):
        state = SerialState()
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_reader = MagicMock()
        mock_reader.mirror_info.return_value = None
        with (
            patch("serial_mcp_server.handlers_serial.pyserial.Serial", return_value=mock_ser),
            patch("serial_mcp_server.handlers_serial.create_reader", return_value=mock_reader),
        ):
            result1 = await handle_open(state, {"port": "/dev/ttyUSB0"})
        assert result1["ok"]

        result2 = await handle_open(state, {"port": "/dev/ttyUSB0"})
        assert not result2["ok"]
        assert result2["error"]["code"] == "already_open"


class TestClose:
    async def test_close(self, connected_entry):
        state, _conn = connected_entry
        result = await handle_close(state, {"connection_id": "s1"})
        assert result["ok"]
        assert result["port"] == "/dev/ttyUSB0"
        assert "s1" not in state.connections

    async def test_close_missing(self):
        state = SerialState()
        with pytest.raises(KeyError):
            await handle_close(state, {"connection_id": "nonexistent"})


class TestConnectionStatus:
    async def test_status(self, connected_entry):
        state, conn = connected_entry
        result = await handle_connection_status(state, {"connection_id": "s1"})
        assert result["ok"]
        assert result["is_open"] is True
        assert result["config"]["baudrate"] == 115200
        assert result["buffered_bytes"] == 0

    async def test_status_with_buffered_data(self, connected_entry):
        state, conn = connected_entry
        conn.buffer.write(b"hello")
        result = await handle_connection_status(state, {"connection_id": "s1"})
        assert result["buffered_bytes"] == 5

    async def test_status_with_mirror(self, connected_entry):
        state, conn = connected_entry
        conn.reader.mirror_info.return_value = {
            "pty_path": "/dev/ttys004",
            "link": None,
            "mode": "ro",
        }
        result = await handle_connection_status(state, {"connection_id": "s1"})
        assert result["mirror"]["pty_path"] == "/dev/ttys004"


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
        with patch.object(threading.Event, "wait", return_value=False) as sleep:
            result = await handle_write(state, {"connection_id": "s1", "data": "ABC"})

        assert result["bytes_written"] == 3
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"A", b"B", b"C"]
        assert [call.args[0] for call in sleep.call_args_list] == [0.008, 0.008]

    async def test_write_with_connection_chunk_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(chunk_size=2, chunk_delay_ms=5)
        conn.ser.write.side_effect = len
        with patch.object(threading.Event, "wait", return_value=False) as sleep:
            result = await handle_write(state, {"connection_id": "s1", "data": "ABCDE"})

        assert result["bytes_written"] == 5
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"AB", b"CD", b"E"]
        assert [call.args[0] for call in sleep.call_args_list] == [0.005, 0.005]

    async def test_write_with_per_call_character_delay_override(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=2)
        conn.ser.write.side_effect = len
        with patch.object(threading.Event, "wait", return_value=False) as sleep:
            result = await handle_write(
                state,
                {"connection_id": "s1", "data": "ABC", "inter_char_delay_ms": 8},
            )

        assert result["bytes_written"] == 3
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"A", b"B", b"C"]
        assert [call.args[0] for call in sleep.call_args_list] == [0.008, 0.008]

    async def test_write_with_per_call_chunk_override_from_character_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=8)
        conn.ser.write.side_effect = len
        with patch.object(threading.Event, "wait", return_value=False) as sleep:
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
        assert [call.args[0] for call in sleep.call_args_list] == [0.005, 0.005]

    async def test_write_zero_override_disables_connection_chunk_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(chunk_size=2, chunk_delay_ms=5)
        conn.ser.write.return_value = 5
        with patch.object(threading.Event, "wait", return_value=False) as sleep:
            result = await handle_write(
                state,
                {"connection_id": "s1", "data": "ABCDE", "inter_char_delay_ms": 0},
            )

        assert result["bytes_written"] == 5
        conn.ser.write.assert_called_once_with(b"ABCDE")
        sleep.assert_not_called()

    async def test_write_zero_character_delay_disables_connection_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=8)
        conn.ser.write.return_value = 5
        with patch.object(threading.Event, "wait", return_value=False) as sleep:
            result = await handle_write(
                state,
                {"connection_id": "s1", "data": "ABCDE", "inter_char_delay_ms": 0},
            )

        assert result["bytes_written"] == 5
        conn.ser.write.assert_called_once_with(b"ABCDE")
        sleep.assert_not_called()

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


class TestControlLines:
    async def test_set_dtr(self, connected_entry):
        state, _conn = connected_entry
        result = await handle_set_dtr(state, {"connection_id": "s1", "value": False})
        assert result["ok"]
        assert result["dtr"] is False

    async def test_set_rts(self, connected_entry):
        state, _conn = connected_entry
        result = await handle_set_rts(state, {"connection_id": "s1", "value": True})
        assert result["ok"]
        assert result["rts"] is True

    async def test_pulse_dtr(self, connected_entry):
        state, conn = connected_entry
        result = await handle_pulse_dtr(state, {"connection_id": "s1", "duration_ms": 10})
        assert result["ok"]
        assert result["duration_ms"] == 10
        # DTR should end high (True) after pulse
        assert conn.ser.dtr is True

    async def test_pulse_rts(self, connected_entry):
        state, conn = connected_entry
        result = await handle_pulse_rts(state, {"connection_id": "s1", "duration_ms": 10})
        assert result["ok"]
        assert result["duration_ms"] == 10
        assert conn.ser.rts is True

    async def test_pulse_dtr_clamped(self, connected_entry):
        state, _conn = connected_entry
        result = await handle_pulse_dtr(state, {"connection_id": "s1", "duration_ms": 999999})
        assert result["ok"]
        assert result["duration_ms"] == 10000


class TestOpenResourceCleanup:
    async def test_open_cleans_up_on_add_connection_failure(self):
        """If add_connection raises (max connections), serial port and reader are closed."""
        state = SerialState(max_connections=0)
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_reader = MagicMock()
        mock_reader.mirror_info.return_value = None
        with (
            patch("serial_mcp_server.handlers_serial.pyserial.Serial", return_value=mock_ser),
            patch("serial_mcp_server.handlers_serial.create_reader", return_value=mock_reader),
            pytest.raises(RuntimeError),
        ):
            await handle_open(state, {"port": "/dev/ttyUSB0"})
        mock_reader.signal_stop.assert_called_once()
        mock_reader.join.assert_called_once()
        mock_ser.close.assert_called_once()


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
    assert await asyncio.to_thread(port.entered.wait, 2)


class TestWriterIntegration:
    async def test_concurrent_writes_do_not_interleave(self, held_entry):
        state, conn, port = held_entry
        args = {"connection_id": "s1", "chunk_size": 2, "chunk_delay_ms": 1}

        first = asyncio.create_task(handle_write(state, {**args, "data": "AAAA"}))
        await _wait_until_held(port)
        second = asyncio.create_task(handle_write(state, {**args, "data": "BBBB"}))
        await asyncio.sleep(0)
        port.release.set()
        results = await asyncio.gather(first, second)

        assert [r["bytes_written"] for r in results] == [4, 4]
        assert port.writes == [b"AA", b"AA", b"BB", b"BB"]

    async def test_cancelling_a_queued_write_sends_nothing(self, held_entry):
        state, conn, port = held_entry

        first = asyncio.create_task(handle_write(state, {"connection_id": "s1", "data": "first"}))
        await _wait_until_held(port)
        queued = asyncio.create_task(handle_write(state, {"connection_id": "s1", "data": "never"}))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        port.release.set()
        await first

        assert port.writes == [b"first"]

    async def test_cancelling_a_running_paced_write_stops_it_between_segments(self, held_entry):
        state, conn, port = held_entry
        args = {"connection_id": "s1", "data": "ABCDEF", "chunk_size": 1, "chunk_delay_ms": 1000}

        task = asyncio.create_task(handle_write(state, args))
        await _wait_until_held(port)
        task.cancel()
        port.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(conn.writer.stop)
        await asyncio.to_thread(conn.writer.join)

        assert port.writes == [b"A"]

    async def test_close_fails_a_queued_write_and_ends_the_writer_thread(self, held_entry):
        state, conn, port = held_entry

        first = asyncio.create_task(handle_write(state, {"connection_id": "s1", "data": "first"}))
        await _wait_until_held(port)
        queued = asyncio.create_task(handle_write(state, {"connection_id": "s1", "data": "queued"}))
        await asyncio.sleep(0)
        stopped = threading.Event()
        real_stop = conn.writer.stop
        conn.writer.stop = lambda: (real_stop(), stopped.set())
        closing = asyncio.create_task(handle_close(state, {"connection_id": "s1"}))
        assert await asyncio.to_thread(stopped.wait, 2)
        port.release.set()
        await closing
        queued_result = await queued
        await first

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

        first = asyncio.create_task(handle_write(state, {"connection_id": "s1", "data": "first"}))
        await _wait_until_held(port)
        queued = asyncio.create_task(handle_write(state, {"connection_id": "s1", "data": "queued"}))
        await asyncio.sleep(0)
        await handle_flush(state, {"connection_id": "s1", "what": "output"})
        port.release.set()
        await first
        queued_result = await queued

        assert queued_result["error"]["code"] == "write_aborted"
        assert queued_result["bytes_written"] == 0
        assert port.writes == [b"first"]
