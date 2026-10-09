"""Tests for the connection tools: list ports, open, close, configure, and connection status."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.tools.connection import (
    HANDLERS,
    TOOLS,
    handle_close,
    handle_configure,
    handle_connection_status,
    handle_list_ports,
    handle_open,
)
from serial_mcp_server.mcp.tools.io import handle_readline, handle_write
from serial_mcp_server.port.connection import stop_connections
from serial_mcp_server.port.pacing import Pacing
from serial_mcp_server.port.stack import open_port_stack


class TestToolRegistration:
    def test_every_tool_has_a_handler(self):
        assert {t.name for t in TOOLS} == set(HANDLERS)

    def test_tools_in_registration_order(self):
        assert [t.name for t in TOOLS] == [
            "serial.list_ports",
            "serial.open",
            "serial.close",
            "serial.connection_status",
            "serial.configure",
        ]


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
            "serial_mcp_server.mcp.tools.connection.serial.tools.list_ports.comports",
            return_value=[mock_port],
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
        with patch(
            "serial_mcp_server.mcp.tools.connection.serial.tools.list_ports.comports", return_value=[]
        ):
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
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_ser),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader),
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
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_ser),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader),
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
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader) as make_reader,
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
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader) as make_reader,
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
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_ser),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader),
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


class TestOpenResourceCleanup:
    async def test_open_cleans_up_on_add_connection_failure(self, settings):
        """If add_connection raises (max connections), serial port and reader are closed."""
        # Zero is not a valid configured value; the copy skips validation to force the failure.
        state = SerialState(settings.model_copy(update={"max_connections": 0}))
        mock_ser = MagicMock()
        mock_ser.is_open = True
        mock_reader = MagicMock()
        mock_reader.mirror_info.return_value = None
        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_ser),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader),
            pytest.raises(RuntimeError),
        ):
            await handle_open(state, {"port": "/dev/ttyUSB0"})
        mock_reader.signal_stop.assert_called_once()
        mock_reader.join.assert_called_once()
        mock_ser.close.assert_called_once()

    async def test_cancelled_open_releases_the_stack_the_worker_finishes_building(
        self, serial_state, mock_serial, mock_reader
    ):
        worker_started = threading.Event()
        proceed = threading.Event()
        stopped = threading.Event()
        stopped_connections = []

        def blocked_open(ser, **kwargs):
            worker_started.set()
            assert proceed.wait(5), "the test never let the worker proceed"
            return open_port_stack(ser, **kwargs)

        def recording_stop(conns):
            stop_connections(conns)
            stopped_connections.extend(conns)
            stopped.set()

        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader),
            patch("serial_mcp_server.mcp.tools.connection.open_port_stack", side_effect=blocked_open),
            patch("serial_mcp_server.mcp.tools.connection.stop_connections", side_effect=recording_stop),
        ):
            task = asyncio.create_task(handle_open(serial_state, {"port": "/dev/ttyUSB0"}))
            assert await asyncio.to_thread(worker_started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            proceed.set()
            assert await asyncio.to_thread(stopped.wait, 5)

        (conn,) = stopped_connections
        assert not conn.writer.alive
        mock_reader.signal_stop.assert_called_once()
        mock_serial.close.assert_called_once()
        assert serial_state.connections == {}

    async def test_cancelled_open_closes_the_port_when_the_worker_fails(self, serial_state, mock_serial):
        worker_started = threading.Event()
        proceed = threading.Event()
        closed = threading.Event()
        mock_serial.close.side_effect = closed.set

        def failing_open(_ser, **_kwargs):
            worker_started.set()
            assert proceed.wait(5), "the test never let the worker proceed"
            raise OSError("the stack could not be built")

        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.mcp.tools.connection.open_port_stack", side_effect=failing_open),
            patch("serial_mcp_server.mcp.tools.connection.stop_connections") as stop,
        ):
            task = asyncio.create_task(handle_open(serial_state, {"port": "/dev/ttyUSB0"}))
            assert await asyncio.to_thread(worker_started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            proceed.set()
            assert await asyncio.to_thread(closed.wait, 5)

        mock_serial.close.assert_called_once()
        stop.assert_not_called()
        assert serial_state.connections == {}


class TestConfigure:
    async def test_chunk_pacing_changes_the_next_write_pattern(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.side_effect = len

        result = await handle_configure(state, {"connection_id": "s1", "chunk_size": 2, "chunk_delay_ms": 1})
        with patch.object(threading.Event, "wait", return_value=False):
            await handle_write(state, {"connection_id": "s1", "data": "ABCDE"})

        assert result["config"]["chunk_size"] == 2
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"AB", b"CD", b"E"]

    async def test_chunk_pacing_applies_to_the_next_mirror_forward(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.side_effect = len

        await handle_configure(state, {"connection_id": "s1", "chunk_size": 2, "chunk_delay_ms": 1})
        with patch.object(threading.Event, "wait", return_value=False):
            await asyncio.wrap_future(conn.writer.submit(b"ABC", source="mirror").future)

        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"AB", b"C"]

    async def test_pacing_arguments_replace_the_whole_pacing_mode(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(chunk_size=2, chunk_delay_ms=5)

        result = await handle_configure(state, {"connection_id": "s1", "inter_char_delay_ms": 8})

        assert conn.writer.pacing == Pacing(inter_char_delay_ms=8)
        assert result["config"]["chunk_size"] == 0

    async def test_zero_disables_pacing(self, connected_entry):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=8)

        await handle_configure(state, {"connection_id": "s1", "inter_char_delay_ms": 0})

        assert conn.writer.pacing == Pacing()

    async def test_newline_changes_the_readline_default_terminator(self, connected_entry):
        state, conn = connected_entry
        conn.buffer.write(b"x\ny\r\nz")

        await handle_configure(state, {"connection_id": "s1", "newline": "\r\n"})
        result = await handle_readline(state, {"connection_id": "s1"})

        # The fixture's default is "\n", which would stop after "x\n".
        assert result["data"] == "x\ny\r\n"

    async def test_connection_status_reports_the_new_values(self, connected_entry):
        state, conn = connected_entry

        await handle_configure(
            state,
            {
                "connection_id": "s1",
                "encoding": "latin-1",
                "newline": "\r",
                "chunk_size": 3,
                "chunk_delay_ms": 7,
            },
        )
        status = await handle_connection_status(state, {"connection_id": "s1"})

        assert status["config"]["encoding"] == "latin-1"
        assert status["config"]["newline"] == repr("\r")
        assert (status["config"]["chunk_size"], status["config"]["chunk_delay_ms"]) == (3, 7)

    @pytest.mark.parametrize(
        "bad",
        [
            {"chunk_size": 3},
            {"inter_char_delay_ms": -1},
            {"inter_char_delay_ms": 8, "chunk_size": 3, "chunk_delay_ms": 5},
            {"encoding": "no-such-codec"},
            {"encoding": "hex"},
            {"newline": ""},
            {"newline": 5},
            {},
        ],
    )
    async def test_invalid_arguments_leave_the_configuration_unchanged(self, connected_entry, bad):
        state, conn = connected_entry
        conn.writer.pacing = Pacing(inter_char_delay_ms=4)
        before = (conn.writer.pacing, conn.encoding, conn.newline)

        result = await handle_configure(state, {"connection_id": "s1", **bad})

        assert result["error"]["code"] == "invalid_params"
        assert (conn.writer.pacing, conn.encoding, conn.newline) == before

    async def test_a_bad_argument_does_not_apply_the_valid_ones(self, connected_entry):
        state, conn = connected_entry

        result = await handle_configure(
            state, {"connection_id": "s1", "newline": "\r", "encoding": "no-such-codec"}
        )

        assert not result["ok"]
        assert conn.newline == "\n"


class TestExposedToolText:
    """The text MCP clients show the agent must follow the behavior; see CONTRIBUTING.md."""

    @staticmethod
    def description(name: str) -> str:
        return next(tool.description for tool in TOOLS if tool.name == name)

    def test_open_tells_the_agent_when_to_log(self):
        text = self.description("serial.open")
        assert "asks to log or record" in text
        assert "log=true" in text

    def test_status_explains_dropped_bytes(self):
        assert "dropped_bytes" in self.description("serial.connection_status")

    def test_configure_points_away_from_reopening(self):
        assert "instead of closing and reopening" in self.description("serial.configure")
