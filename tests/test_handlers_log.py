"""Tests for the serial.log.* tools and the read-buffer drop counter."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from serial_mcp_server import config
from serial_mcp_server.config import Settings
from serial_mcp_server.handlers_log import (
    HANDLERS,
    TOOLS,
    handle_log_mark,
    handle_log_start,
    handle_log_status,
    handle_log_stop,
)
from serial_mcp_server.handlers_serial import handle_connection_status, handle_open
from serial_mcp_server.port.buffer import DEFAULT_BUFFER_BYTES, SerialBuffer
from serial_mcp_server.port.stack import make_read_buffer
from serial_mcp_server.portlog.formats import BUFFER_DROP_EVENT
from serial_mcp_server.portlog.log import PortLog
from tests.helpers import read_jsonl, reconfigure

SID = {"connection_id": "s1"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Default settings with the data root at tmp_path/spec; serial_state uses them."""
    return config.load({"SERIAL_MCP_SPEC_ROOT": str(tmp_path / "spec")})


@pytest.fixture
def log_env(settings: Settings):
    return settings.log_dir


def files_by_suffix(directory) -> dict:
    return {p.name.split(".", 1)[1]: p for p in directory.iterdir()}


class TestRegistration:
    def test_every_tool_has_a_handler_and_a_connection_id(self):
        assert {t.name for t in TOOLS} == set(HANDLERS)
        assert all("connection_id" in t.inputSchema["required"] for t in TOOLS)

    def test_start_description_tells_the_agent_when_to_use_it(self):
        start = next(t for t in TOOLS if t.name == "serial.log.start")
        assert "user asks to log or record" in start.description


class TestStart:
    async def test_starts_text_logging_by_default_and_reports_the_paths(self, connected_entry, log_env):
        state, conn = connected_entry

        result = await handle_log_start(state, SID)

        assert result["ok"]
        assert list(result["log"]["files"]) == ["text"]
        assert sorted(files_by_suffix(log_env)) == ["txt"]
        await handle_log_stop(state, SID)

    async def test_starting_twice_returns_already_logging_and_creates_no_second_file(
        self, connected_entry, log_env
    ):
        state, _ = connected_entry
        await handle_log_start(state, SID)

        again = await handle_log_start(state, SID)

        assert again["error"]["code"] == "already_logging"
        assert len(list(log_env.iterdir())) == 1
        await handle_log_stop(state, SID)

    async def test_formats_and_timestamps_are_taken_from_the_arguments(self, connected_entry, log_env):
        state, conn = connected_entry

        await handle_log_start(
            state, {**SID, "log_formats": ["text", "lines"], "log_text_timestamps": "elapsed"}
        )
        conn.log_slot.rx(b"hello\n")
        await handle_log_stop(state, SID)

        files = files_by_suffix(log_env)
        assert sorted(files) == ["lines.jsonl", "txt"]
        assert files["txt"].read_text().startswith("[0.000000 0.000000] hello")

    async def test_the_first_logged_event_is_open_with_the_connection_config(self, connected_entry, log_env):
        state, _ = connected_entry
        await handle_log_start(state, {**SID, "log_formats": ["jsonl"]})
        await handle_log_stop(state, SID)

        first = read_jsonl(files_by_suffix(log_env)["jsonl"])[0]
        assert (first["event"], first["config"]["port"]) == ("open", "/dev/ttyUSB0")

    @pytest.mark.parametrize("args", [{"log_formats": ["xml"]}, {"log_text_timestamps": "fast"}])
    async def test_invalid_arguments_start_nothing(self, connected_entry, log_env, args):
        state, conn = connected_entry

        result = await handle_log_start(state, {**SID, **args})

        assert result["error"]["code"] == "invalid_params"
        assert conn.log_slot.log is None
        assert not log_env.exists()

    async def test_directory_failure_is_reported_and_leaves_the_connection_unchanged(
        self, connected_entry, tmp_path, monkeypatch
    ):
        state, conn = connected_entry
        blocker = tmp_path / "file"
        blocker.write_text("x")
        reconfigure(state, log_dir=blocker / "logs")

        result = await handle_log_start(state, SID)

        assert result["error"]["code"] == "log_failed"
        assert conn.log_slot.log is None

    async def test_a_log_can_start_again_after_stop_with_new_files(self, connected_entry, log_env):
        state, _ = connected_entry
        await handle_log_start(state, SID)
        await handle_log_stop(state, SID)

        again = await handle_log_start(state, SID)
        await handle_log_stop(state, SID)

        assert again["ok"]
        assert len(list(log_env.iterdir())) == 2


class TestStop:
    async def test_returns_paths_and_byte_counts_and_closes_the_files(self, connected_entry):
        state, conn = connected_entry
        await handle_log_start(state, {**SID, "log_formats": ["text", "raw"]})
        conn.log_slot.rx(b"12345")

        result = await handle_log_stop(state, SID)

        sizes = {f["format"]: f["bytes"] for f in result["files"]}
        assert sizes == {"text": 5, "raw": 5}
        assert result["entries"] == 3  # the open event, one received chunk, and the close event
        assert conn.log_slot.log is None

    async def test_not_logging_is_an_error(self, connected_entry):
        state, _ = connected_entry
        result = await handle_log_stop(state, SID)
        assert result["error"]["code"] == "not_logging"

    async def test_bytes_after_stop_are_not_logged(self, connected_entry, log_env):
        state, conn = connected_entry
        await handle_log_start(state, SID)
        conn.log_slot.rx(b"before")
        await handle_log_stop(state, SID)

        conn.log_slot.rx(b"after")

        assert files_by_suffix(log_env)["txt"].read_bytes() == b"before"


class TestStatus:
    async def test_reports_not_logging(self, connected_entry):
        state, _ = connected_entry
        result = await handle_log_status(state, SID)
        assert (result["ok"], result["logging"]) == (True, False)

    async def test_reports_formats_files_and_counts_while_logging(self, connected_entry):
        state, conn = connected_entry
        await handle_log_start(state, {**SID, "log_formats": ["text", "jsonl"]})
        conn.log_slot.rx(b"abc")

        result = await handle_log_status(state, SID)
        await handle_log_stop(state, SID)

        assert result["logging"] is True
        assert result["formats"] == ["text", "jsonl"]
        assert set(result["files"]) == {"text", "jsonl"}
        assert result["dropped_bytes"] == 0


class TestMark:
    async def test_mark_lands_between_traffic_in_jsonl_and_lines_only(self, connected_entry, log_env):
        state, conn = connected_entry
        await handle_log_start(state, {**SID, "log_formats": ["text", "jsonl", "lines", "raw"]})
        conn.log_slot.rx(b"before\n")
        mark = await handle_log_mark(state, {**SID, "label": "step 2"})
        conn.log_slot.rx(b"after\n")
        await handle_log_stop(state, SID)

        files = files_by_suffix(log_env)
        assert mark["ok"]
        jsonl = [e.get("event") or e.get("dir") for e in read_jsonl(files["jsonl"])]
        assert jsonl == ["open", "rx", "mark", "rx", "close"]
        lines = [(e.get("event"), e.get("text"), e.get("label")) for e in read_jsonl(files["lines.jsonl"])]
        assert lines == [(None, "before", None), ("mark", None, "step 2"), (None, "after", None)]
        assert files["txt"].read_bytes() == files["bin"].read_bytes() == b"before\nafter\n"

    async def test_not_logging_is_an_error(self, connected_entry):
        state, _ = connected_entry
        result = await handle_log_mark(state, {**SID, "label": "x"})
        assert result["error"]["code"] == "not_logging"

    async def test_label_must_be_a_string(self, connected_entry):
        state, _ = connected_entry
        await handle_log_start(state, SID)

        result = await handle_log_mark(state, {**SID, "label": 5})
        await handle_log_stop(state, SID)

        assert result["error"]["code"] == "invalid_params"


class TestBufferDrops:
    def test_buffer_counts_the_exact_bytes_it_trims(self):
        buf = SerialBuffer(max_size=10)

        buf.write(b"a" * 8)
        assert buf.dropped_bytes == 0
        buf.write(b"b" * 5)
        buf.write(b"c" * 4)

        assert buf.dropped_bytes == 7
        assert buf.available == 10

    def test_on_drop_receives_each_trim_after_the_lock_is_released(self):
        seen = []
        buf = SerialBuffer(max_size=4, on_drop=lambda n: seen.append((n, buf.available)))

        buf.write(b"abcdef")  # calling back into the buffer here would deadlock if the lock were held
        buf.write(b"gh")

        assert seen == [(2, 4), (2, 4)]

    async def test_more_than_the_limit_unread_reports_dropped_bytes_and_logs_buffer_drop(
        self, connected_entry, log_env
    ):
        state, conn = connected_entry
        conn.buffer = make_read_buffer(conn.log_slot)  # the buffer serial.open builds
        await handle_log_start(state, {**SID, "log_formats": ["jsonl"]})
        limit = DEFAULT_BUFFER_BYTES

        conn.buffer.write(b"x" * limit)
        conn.buffer.write(b"y" * 300)
        status = await handle_connection_status(state, SID)
        await handle_log_stop(state, SID)

        assert status["dropped_bytes"] == 300
        drops = [
            e for e in read_jsonl(files_by_suffix(log_env)["jsonl"]) if e.get("event") == BUFFER_DROP_EVENT
        ]
        assert [d["dropped_bytes"] for d in drops] == [300]


class TestStartRaces:
    async def test_two_concurrent_starts_leave_exactly_one_log(self, connected_entry, log_env):
        state, _ = connected_entry

        first, second = await asyncio.gather(handle_log_start(state, SID), handle_log_start(state, SID))

        assert sorted(r["ok"] for r in (first, second)) == [False, True]
        loser = first if not first["ok"] else second
        assert loser["error"]["code"] == "already_logging"
        assert len(list(log_env.iterdir())) == 1
        await handle_log_stop(state, SID)

    async def test_closing_the_connection_during_start_leaves_no_live_log(self, connected_entry, log_env):
        state, conn = connected_entry
        real_start = PortLog.start

        def start_then_close(*args, **kwargs):
            log = real_start(*args, **kwargs)
            state.remove_connection("s1")  # the close wins the race while the files are being created
            return log

        with patch.object(PortLog, "start", side_effect=start_then_close):
            result = await handle_log_start(state, SID)

        assert result["error"]["code"] == "not_found"
        assert conn.log_slot.log is None
        assert list(log_env.iterdir()) == []
        assert not [t for t in threading.enumerate() if t.name == "serial-portlog" and t.is_alive()]

    async def test_the_open_event_precedes_traffic_that_arrives_right_after_attach(
        self, connected_entry, log_env
    ):
        state, conn = connected_entry
        await handle_log_start(state, {**SID, "log_formats": ["jsonl"]})
        conn.log_slot.rx(b"x")
        await handle_log_stop(state, SID)

        assert [e.get("event") or e.get("dir") for e in read_jsonl(files_by_suffix(log_env)["jsonl"])] == [
            "open",
            "rx",
            "close",
        ]


class TestOpenWiresBufferDrops:
    async def test_a_port_opened_with_a_log_reports_and_logs_dropped_bytes(
        self, serial_state, mock_serial, mock_reader, log_env
    ):
        with (
            patch("serial_mcp_server.handlers_serial.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader),
        ):
            opened = await handle_open(serial_state, {"port": "/dev/ttyUSB0", "log_formats": ["jsonl"]})
        sid = {"connection_id": opened["connection_id"]}
        buffer = serial_state.get_connection(sid["connection_id"]).buffer

        buffer.write(b"x" * DEFAULT_BUFFER_BYTES)
        buffer.write(b"y" * 123)
        status = await handle_connection_status(serial_state, sid)
        await handle_log_stop(serial_state, sid)

        assert status["dropped_bytes"] == 123
        drops = [
            e for e in read_jsonl(files_by_suffix(log_env)["jsonl"]) if e.get("event") == BUFFER_DROP_EVENT
        ]
        assert [d["dropped_bytes"] for d in drops] == [123]
