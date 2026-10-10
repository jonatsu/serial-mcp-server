"""Tests for the connection tools: list ports, open, close, configure, and connection status."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import anyio
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
from tests.helpers import skipped_timed_waits


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

    @pytest.mark.parametrize("timeout_ms", [0, 10_000])
    async def test_the_port_read_timeout_stays_fixed_whatever_timeout_ms_is(
        self, serial_state, mock_serial, mock_reader, timeout_ms
    ):
        with (
            patch(
                "serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial
            ) as open_port,
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader),
        ):
            result = await handle_open(serial_state, {"port": "/dev/ttyUSB0", "timeout_ms": timeout_ms})

        assert result["ok"]
        assert open_port.call_args.kwargs["timeout"] == 0.2  # zero would make pyserial non-blocking
        assert result["config"]["timeout_ms"] == timeout_ms
        assert serial_state.connections[result["connection_id"]].timeout == timeout_ms / 1000

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

    # The MCP SDK cancels a request through the request's AnyIO cancel scope, so these tests cancel the same way.
    # A task cancelled directly with Task.cancel(), outside any cancel scope, goes through shielded scopes.

    async def test_cancelled_open_waits_for_the_stack_then_releases_it(
        self, serial_state, mock_serial, mock_reader
    ):
        worker_started = threading.Event()
        proceed = threading.Event()
        events: list[str] = []

        def blocked_open(ser, **kwargs):
            worker_started.set()
            assert proceed.wait(5), "the test never let the worker proceed"
            return open_port_stack(ser, **kwargs)

        def recording_stop(conns):
            stop_connections(conns)
            events.append("stack stopped")

        request_scope = anyio.CancelScope()

        async def request() -> None:
            with request_scope:
                await handle_open(serial_state, {"port": "/dev/ttyUSB0"})
            events.append("request ended")

        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader),
            patch("serial_mcp_server.mcp.tools.connection.open_port_stack", side_effect=blocked_open),
            patch("serial_mcp_server.mcp.tools.connection.stop_connections", side_effect=recording_stop),
        ):
            async with anyio.create_task_group() as tg:
                tg.start_soon(request)
                assert await anyio.to_thread.run_sync(worker_started.wait, 5)
                request_scope.cancel()
                await anyio.sleep(0.05)
                assert events == [], "the request must wait for the worker it started"
                proceed.set()

        assert events == ["stack stopped", "request ended"]
        assert request_scope.cancelled_caught
        mock_reader.signal_stop.assert_called_once()
        mock_serial.close.assert_called_once()
        assert serial_state.connections == {}

    async def test_cancelled_open_closes_the_port_when_the_worker_fails(self, serial_state, mock_serial):
        worker_started = threading.Event()
        proceed = threading.Event()
        events: list[str] = []
        mock_serial.close.side_effect = lambda: events.append("port closed")

        def failing_open(_ser, **_kwargs):
            worker_started.set()
            assert proceed.wait(5), "the test never let the worker proceed"
            raise OSError("the stack could not be built")

        request_scope = anyio.CancelScope()

        async def request() -> None:
            with pytest.raises(OSError, match="could not be built"), request_scope:
                await handle_open(serial_state, {"port": "/dev/ttyUSB0"})
            events.append("request ended")

        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.mcp.tools.connection.open_port_stack", side_effect=failing_open),
            patch("serial_mcp_server.mcp.tools.connection.stop_connections") as stop,
        ):
            async with anyio.create_task_group() as tg:
                tg.start_soon(request)
                assert await anyio.to_thread.run_sync(worker_started.wait, 5)
                request_scope.cancel()
                proceed.set()

        assert events == ["port closed", "request ended"]
        stop.assert_not_called()
        assert serial_state.connections == {}

    async def test_open_cancelled_during_its_cleanup_still_closes_the_port_before_it_ends(
        self, serial_state, mock_serial
    ):
        closing = threading.Event()
        release_close = threading.Event()
        events: list[str] = []

        def slow_close() -> None:
            closing.set()
            assert release_close.wait(5), "the test never released the close"
            events.append("port closed")

        mock_serial.close.side_effect = slow_close
        request_scope = anyio.CancelScope()

        async def request() -> None:
            with pytest.raises(OSError, match="no stack"), request_scope:
                await handle_open(serial_state, {"port": "/dev/ttyUSB0"})
            events.append("request ended")

        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.mcp.tools.connection.open_port_stack", side_effect=OSError("no stack")),
        ):
            async with anyio.create_task_group() as tg:
                tg.start_soon(request)
                assert await anyio.to_thread.run_sync(closing.wait, 5)
                request_scope.cancel()
                await anyio.sleep(0.05)  # let the cancellation reach the cleanup's await
                release_close.set()

        assert events == ["port closed", "request ended"]

    async def test_cancelled_open_closes_a_port_whose_stack_never_started(self, serial_state, mock_serial):
        opening = threading.Event()
        proceed = threading.Event()

        def slow_serial(**_kwargs):
            opening.set()
            assert proceed.wait(5), "the test never let the port open"
            return mock_serial

        request_scope = anyio.CancelScope()

        async def request() -> None:
            with request_scope:
                await handle_open(serial_state, {"port": "/dev/ttyUSB0"})

        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", side_effect=slow_serial),
            patch("serial_mcp_server.mcp.tools.connection.open_port_stack") as build,
        ):
            async with anyio.create_task_group() as tg:
                tg.start_soon(request)
                assert await anyio.to_thread.run_sync(opening.wait, 5)
                request_scope.cancel()
                proceed.set()

        assert request_scope.cancelled_caught
        build.assert_not_called()
        mock_serial.close.assert_called_once()
        assert serial_state.connections == {}


class TestConfigure:
    async def test_chunk_pacing_changes_the_next_write_pattern(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.side_effect = len

        result = await handle_configure(state, {"connection_id": "s1", "chunk_size": 2, "chunk_delay_ms": 1})
        with skipped_timed_waits():
            await handle_write(state, {"connection_id": "s1", "data": "ABCDE"})

        assert result["config"]["chunk_size"] == 2
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"AB", b"CD", b"E"]

    async def test_chunk_pacing_applies_to_the_next_mirror_forward(self, connected_entry):
        state, conn = connected_entry
        conn.ser.write.side_effect = len

        await handle_configure(state, {"connection_id": "s1", "chunk_size": 2, "chunk_delay_ms": 1})
        with skipped_timed_waits():
            await anyio.to_thread.run_sync(conn.writer.submit(b"ABC", source="mirror").future.result)

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
        return next(tool.description or "" for tool in TOOLS if tool.name == name)

    def test_open_tells_the_agent_when_to_log(self):
        text = self.description("serial.open")
        assert "asks to log or record" in text
        assert "log=true" in text

    def test_status_explains_dropped_bytes(self):
        assert "dropped_bytes" in self.description("serial.connection_status")

    def test_configure_points_away_from_reopening(self):
        assert "instead of closing and reopening" in self.description("serial.configure")
