"""Port log wiring: serial.open settings, taps in the reader and writer, and control events."""

from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch

import anyio
import anyio.to_thread
import pytest

from serial_mcp_server import config
from serial_mcp_server.config import Settings
from serial_mcp_server.mcp.tools.connection import (
    handle_close,
    handle_configure,
    handle_connection_status,
    handle_open,
)
from serial_mcp_server.mcp.tools.io import handle_flush, handle_write
from serial_mcp_server.mcp.tools.lines import handle_pulse_dtr, handle_set_dtr, handle_set_rts
from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.mirror import MirrorSession
from serial_mcp_server.port.pacing import Pacing
from serial_mcp_server.port.reader import ReaderThread
from serial_mcp_server.port.writer import SerialWriter
from serial_mcp_server.portlog import formats as log_formats
from serial_mcp_server.portlog.log import PortLog, PortLogSlot
from serial_mcp_server.portlog.request import LogRequest
from tests.helpers import read_jsonl, reconfigure, skipped_timed_waits
from tests.port.test_mirror import _PipePort

WAIT_S = 2.0
_IS_UNIX = sys.platform != "win32"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Default settings with the data root at tmp_path/spec; serial_state uses them."""
    return config.load({"SERIAL_MCP_SPEC_ROOT": str(tmp_path / "spec")})


@pytest.fixture
def log_env(settings: Settings):
    """The log directory under the test's data root; no logging variables are set."""
    return settings.log_dir


async def open_port(state, mock_serial, mock_reader, **args):
    """Run handle_open with the serial layer faked; return (result, create_reader mock)."""
    with (
        patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
        patch("serial_mcp_server.port.stack.create_reader", return_value=mock_reader) as make_reader,
    ):
        result = await handle_open(state, {"port": "/dev/ttyUSB0", **args})
    return result, make_reader


async def close_port(state, result):
    await handle_close(state, {"connection_id": result["connection_id"]})


class TestOpenSettings:
    async def test_no_variables_create_no_log_directory_or_file(
        self, serial_state, mock_serial, mock_reader, log_env
    ):
        result, _ = await open_port(serial_state, mock_serial, mock_reader)
        await close_port(serial_state, result)

        assert result["ok"]
        assert "log" not in result
        assert not log_env.exists()
        assert not log_env.parent.exists()

    async def test_environment_enables_one_text_file_identical_to_the_received_bytes(
        self, serial_state, mock_serial, mock_reader, log_env, monkeypatch
    ):
        reconfigure(serial_state, log_enabled=True)
        result, make_reader = await open_port(serial_state, mock_serial, mock_reader)
        slot = make_reader.call_args.kwargs["log_slot"]
        for chunk in (b"boot\r\n", b"\xff\xfe raw", b"> "):
            slot.rx(chunk)
        await close_port(serial_state, result)

        (path,) = log_env.iterdir()
        assert path.suffix == ".txt"
        assert path.read_bytes() == b"boot\r\n\xff\xfe raw> "
        assert result["log"]["files"] == {"text": str(path)}

    @pytest.mark.parametrize(
        ("env", "args", "expected"),
        [
            ("1", {"log": False}, []),
            ("", {"log": True}, [".txt"]),
            ("", {"log_formats": ["jsonl"]}, [".jsonl"]),
            ("1", {"log": False, "log_formats": ["jsonl"]}, []),
            ("1", {"log_formats": ["text", "jsonl"]}, [".jsonl", ".txt"]),
            ("", {"log": "true"}, [".txt"]),
            ("1", {"log": "false"}, []),
        ],
    )
    async def test_arguments_override_the_environment(
        self, serial_state, mock_serial, mock_reader, log_env, monkeypatch, env, args, expected
    ):
        if env:
            reconfigure(serial_state, log_enabled=True)
        result, _ = await open_port(serial_state, mock_serial, mock_reader, **args)
        await close_port(serial_state, result)

        assert result["ok"]
        files = sorted(p.suffix for p in log_env.iterdir()) if log_env.exists() else []
        assert files == expected

    async def test_invalid_formats_are_rejected_before_the_port_opens(
        self, serial_state, mock_serial, mock_reader
    ):
        with patch(
            "serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial
        ) as serial_cls:
            result = await handle_open(serial_state, {"port": "/dev/ttyUSB0", "log_formats": ["xml"]})

        assert result["error"]["code"] == "invalid_params"
        serial_cls.assert_not_called()

    async def test_log_setup_failure_closes_the_port_and_returns_the_error(
        self, serial_state, mock_serial, mock_reader, tmp_path, monkeypatch
    ):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x")
        reconfigure(serial_state, log_dir=blocker / "logs")

        result, make_reader = await open_port(serial_state, mock_serial, mock_reader, log=True)

        assert result["error"]["code"] == "log_failed"
        mock_serial.close.assert_called_once()
        make_reader.assert_not_called()
        assert serial_state.connections == {}

    async def test_connection_status_reports_the_log_files(self, serial_state, mock_serial, mock_reader):
        result, _ = await open_port(serial_state, mock_serial, mock_reader, log=True)
        status = await handle_connection_status(serial_state, {"connection_id": result["connection_id"]})
        await close_port(serial_state, result)

        assert status["log"]["files"] == result["log"]["files"]


class LoggedConnection:
    """A connection from the ``connected_entry`` fixture with a jsonl log attached."""

    def __init__(self, state, conn, tmp_path):
        self.state, self.conn = state, conn
        self.log = PortLog.start(LogRequest(("jsonl",), tmp_path / "logs"), "utf-8")
        conn.log_slot.set(self.log)

    def entries(self) -> list[dict]:
        """Everything logged so far except the closing event that ending the log adds."""
        self.conn.log_slot.close_log()
        return [e for e in read_jsonl(self.log.files["jsonl"]) if e.get("event") != "close"]


@pytest.fixture()
def logged(connected_entry, tmp_path):
    state, conn = connected_entry
    return LoggedConnection(state, conn, tmp_path)


class TestControlEvents:
    async def test_dtr_rts_flush_and_close_each_produce_their_event(self, logged):
        sid = {"connection_id": "s1"}
        await handle_set_dtr(logged.state, {**sid, "value": False})
        await handle_set_rts(logged.state, {**sid, "value": True})
        await handle_flush(logged.state, {**sid, "what": "input"})
        await handle_close(logged.state, sid)

        events = [
            (e["event"], {k: v for k, v in e.items() if k not in ("ts", "t", "event")})
            for e in read_jsonl(logged.log.files["jsonl"])
        ]
        assert events == [
            ("dtr", {"value": False}),
            ("rts", {"value": True}),
            ("flush", {"what": "input"}),
            ("close", {}),
        ]

    async def test_pulse_logs_the_low_then_high_transition(self, logged):
        await handle_pulse_dtr(logged.state, {"connection_id": "s1", "duration_ms": 1})

        assert [(e["event"], e["value"]) for e in logged.entries()] == [("dtr", False), ("dtr", True)]

    async def test_configure_logs_the_new_settings(self, logged):
        await handle_configure(logged.state, {"connection_id": "s1", "chunk_size": 2, "chunk_delay_ms": 3})

        (event,) = logged.entries()
        assert (event["event"], event["chunk_size"], event["chunk_delay_ms"]) == ("configure", 2, 3)

    async def test_open_event_records_the_connection_config(
        self, serial_state, mock_serial, mock_reader, log_env
    ):
        result, _ = await open_port(
            serial_state, mock_serial, mock_reader, log_formats=["jsonl"], baudrate=9600
        )
        await close_port(serial_state, result)

        (path,) = log_env.iterdir()
        first, *_, last = read_jsonl(path)
        assert (first["event"], first["config"]["baudrate"], first["config"]["port"]) == (
            "open",
            9600,
            "/dev/ttyUSB0",
        )
        assert last["event"] == "close"


class TestTransmitTap:
    async def test_paced_write_logs_one_tx_entry_per_segment_in_order(self, logged):
        logged.conn.ser.write.side_effect = len

        with skipped_timed_waits():
            await handle_write(
                logged.state, {"connection_id": "s1", "data": "ABCDE", "chunk_size": 2, "chunk_delay_ms": 1}
            )

        tx = [e for e in logged.entries() if e.get("dir") == "tx"]
        assert [(e["text"], e["source"]) for e in tx] == [("AB", "mcp"), ("CD", "mcp"), ("E", "mcp")]
        assert [e["t"] for e in tx] == sorted(e["t"] for e in tx)

    async def test_mirror_forward_logs_with_the_mirror_source(self, logged):
        logged.conn.ser.write.side_effect = len

        with skipped_timed_waits():
            job = logged.conn.writer.submit(b"abc", Pacing(chunk_size=2, chunk_delay_ms=1), source="mirror")
            await anyio.to_thread.run_sync(job.future.result)

        tx = [e for e in logged.entries() if e.get("dir") == "tx"]
        assert [(e["text"], e["source"]) for e in tx] == [("ab", "mirror"), ("c", "mirror")]

    async def test_only_accepted_bytes_are_logged(self, logged):
        logged.conn.ser.write.return_value = 2

        await handle_write(logged.state, {"connection_id": "s1", "data": "ABCDE"})

        (tx,) = [e for e in logged.entries() if e.get("dir") == "tx"]
        assert tx["text"] == "AB"


class TestReceiveTap:
    def start(self, port, tmp_path, make_reader):
        """Start a log, writer, and reader on *port*; ``make_reader(buf, slot, writer)`` builds the reader."""
        slot = PortLogSlot()
        log = PortLog.start(LogRequest(("jsonl",), tmp_path / "logs"), "utf-8")
        slot.set(log)
        buf = SerialBuffer()
        writer = SerialWriter(port, log_slot=slot)
        reader = make_reader(buf, slot, writer)
        writer.start()
        reader.start()
        return log, buf, writer, reader

    def receive(self, port, buf, payload: bytes) -> bytes:
        os.write(port.rx_write, payload)
        received = b""
        deadline = time.monotonic() + WAIT_S
        while len(received) < len(payload) and time.monotonic() < deadline:
            received += buf.read(100, timeout=0.05)
        return received

    def logged_rx(self, log: PortLog) -> bytes:
        log.close()
        return b"".join(
            bytes.fromhex(e["hex"]) for e in read_jsonl(log.files["jsonl"]) if e.get("dir") == "rx"
        )

    def shut_down(self, port, writer, reader):
        writer.stop()
        reader.signal_stop()
        writer.join()
        reader.join()
        port.close_pipe()

    @pytest.mark.skipif(not _IS_UNIX, reason="the fake port selects on a pipe")
    def test_plain_reader_logs_each_byte_once(self, tmp_path):
        port = _PipePort()
        log, buf, writer, reader = self.start(
            port, tmp_path, lambda buf, slot, writer: ReaderThread(port, buf, slot)
        )
        try:
            received = self.receive(port, buf, b"hello \xff device")
        finally:
            self.shut_down(port, writer, reader)

        assert self.logged_rx(log) == received == b"hello \xff device"

    @pytest.mark.skipif(not _IS_UNIX, reason="PTY mirror requires Unix")
    @pytest.mark.parametrize("mode", ["ro", "rw"])
    def test_mirror_logs_each_byte_once(self, tmp_path, mode):
        port = _PipePort()
        log, buf, writer, reader = self.start(
            port,
            tmp_path,
            lambda buf, slot, writer: MirrorSession(port, buf, mode=mode, writer=writer, log_slot=slot),
        )
        try:
            received = self.receive(port, buf, b"mirror data")
        finally:
            self.shut_down(port, writer, reader)

        assert self.logged_rx(log) == received == b"mirror data"


class TestOpenSchemaAndFailures:
    def test_log_arguments_are_top_level_properties_of_serial_open(self):
        from serial_mcp_server.mcp.tools.connection import TOOLS

        schema = next(t for t in TOOLS if t.name == "serial.open").inputSchema["properties"]

        assert {"log", "log_formats"} <= set(schema)
        assert set(schema["newline"]) == {"type", "default", "description"}
        assert schema["log_formats"]["items"]["enum"] == list(log_formats.FORMATS)

    @pytest.mark.parametrize("encoding", ["no-such-codec", "hex", 5])
    async def test_unusable_encoding_is_rejected_before_the_port_opens(
        self, serial_state, mock_serial, mock_reader, encoding
    ):
        with patch(
            "serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial
        ) as serial_cls:
            result = await handle_open(
                serial_state, {"port": "/dev/ttyUSB0", "log": True, "encoding": encoding}
            )

        assert result["error"]["code"] == "invalid_params"
        serial_cls.assert_not_called()

    async def test_open_event_comes_before_bytes_the_reader_delivers_at_startup(
        self, serial_state, mock_serial, mock_reader, log_env
    ):
        slots = []
        mock_reader.start.side_effect = lambda: slots[0].rx(b"banner")  # a device that talks at once

        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch(
                "serial_mcp_server.port.stack.create_reader",
                side_effect=lambda *a, log_slot, **k: slots.append(log_slot) or mock_reader,
            ),
        ):
            result = await handle_open(serial_state, {"port": "/dev/ttyUSB0", "log_formats": ["jsonl"]})
        await close_port(serial_state, result)

        (path,) = log_env.iterdir()
        assert [e.get("event") or e.get("dir") for e in read_jsonl(path)] == ["open", "rx", "close"]

    async def test_failed_reader_creation_closes_the_log_and_the_port(
        self, serial_state, mock_serial, log_env
    ):
        before = _log_threads()
        with (
            patch("serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial),
            patch("serial_mcp_server.port.stack.create_reader", side_effect=OSError("no ptys")),
            pytest.raises(OSError, match="no ptys"),
        ):
            await handle_open(serial_state, {"port": "/dev/ttyUSB0", "log": True})

        mock_serial.close.assert_called_once()
        assert serial_state.connections == {}
        (path,) = log_env.iterdir()
        assert path.read_bytes() == b""  # closed cleanly, nothing was received
        assert _log_threads() <= before


def _log_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == "serial-portlog" and t.is_alive()}


class TestOpenFormatsAndTimestamps:
    async def test_all_four_formats_agree_on_the_received_bytes_and_line_boundaries(
        self, serial_state, mock_serial, mock_reader, log_env
    ):
        result, make_reader = await open_port(
            serial_state, mock_serial, mock_reader, log_formats=["text", "jsonl", "lines", "raw"]
        )
        slot = make_reader.call_args.kwargs["log_slot"]
        for chunk in (b"boot\r\nlog", b"in: \xff\n", b"> "):
            slot.rx(chunk)
        await close_port(serial_state, result)

        by_suffix = {p.name.split(".", 1)[1]: p for p in log_env.iterdir()}
        assert sorted(by_suffix) == ["bin", "jsonl", "lines.jsonl", "txt"]
        received = b"boot\r\nlogin: \xff\n> "
        assert by_suffix["bin"].read_bytes() == by_suffix["txt"].read_bytes() == received
        jsonl_rx = b"".join(
            bytes.fromhex(e["hex"]) for e in read_jsonl(by_suffix["jsonl"]) if e.get("dir") == "rx"
        )
        assert jsonl_rx == received
        assert [(e["text"], e.get("partial")) for e in read_jsonl(by_suffix["lines.jsonl"])] == [
            ("boot", None),
            ("login: �", None),
            ("> ", True),
        ]

    async def test_timestamps_argument_prefixes_the_text_log(
        self, serial_state, mock_serial, mock_reader, log_env
    ):
        result, make_reader = await open_port(
            serial_state, mock_serial, mock_reader, log=True, log_text_timestamps="elapsed"
        )
        make_reader.call_args.kwargs["log_slot"].rx(b"hello\n")
        await close_port(serial_state, result)

        (path,) = log_env.iterdir()
        assert path.read_text().startswith("[0.000000 0.000000] hello\n")

    async def test_invalid_timestamp_mode_is_rejected_before_the_port_opens(self, serial_state, mock_serial):
        with patch(
            "serial_mcp_server.mcp.tools.connection.pyserial.Serial", return_value=mock_serial
        ) as serial_cls:
            result = await handle_open(
                serial_state, {"port": "/dev/ttyUSB0", "log": True, "log_text_timestamps": "fast"}
            )

        assert result["error"]["code"] == "invalid_params"
        serial_cls.assert_not_called()
