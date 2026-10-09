"""Tests for serial.send_file — what reaches the port, what is refused, and what the log shows."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
import serial as pyserial

from serial_mcp_server import paths
from serial_mcp_server.handlers_file import HANDLERS, MAX_SEND_FILE_BYTES, TOOLS, handle_send_file
from serial_mcp_server.portlog import LogRequest, PortLog
from serial_mcp_server.writer import Pacing
from tests.helpers import read_jsonl, writer_goes_idle

SID = {"connection_id": "s1"}
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks and FIFOs")


@pytest.fixture()
def files(tmp_path, monkeypatch):
    directory = tmp_path / "files"
    directory.mkdir()
    monkeypatch.setenv(paths.ENV_FILES_DIR, str(directory))
    monkeypatch.delenv("SERIAL_MCP_MAX_WRITE_SECONDS", raising=False)
    return directory


def written(conn) -> list[bytes]:
    return [call.args[0] for call in conn.ser.write.call_args_list]


class TestRegistration:
    def test_tool_and_handler_are_registered_together(self):
        assert [t.name for t in TOOLS] == list(HANDLERS) == ["serial.send_file"]
        assert TOOLS[0].inputSchema["required"] == ["connection_id", "path"]

    def test_description_tells_the_agent_when_and_where(self):
        text = TOOLS[0].description
        assert text.startswith("Use for long inputs")
        assert "SERIAL_MCP_FILES_DIR" in text
        assert "1 MiB" in text


class TestSending:
    async def test_a_file_is_written_byte_exact_with_the_chosen_pacing(self, connected_entry, files):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        (files / "env.txt").write_bytes(b"setenv a b\n\xff\x00end")

        with patch.object(threading.Event, "wait", return_value=False):
            result = await handle_send_file(
                state, {**SID, "path": "env.txt", "chunk_size": 6, "chunk_delay_ms": 1}
            )

        assert result["ok"]
        assert result["bytes_written"] == 16
        assert written(conn) == [b"setenv", b" a b\n\xff", b"\x00end"]
        assert result["path"] == "env.txt"

    async def test_an_unpaced_file_goes_out_in_4_kib_segments(self, connected_entry, files):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        content = bytes(range(256)) * 40  # 10,240 bytes
        (files / "big.bin").write_bytes(content)

        result = await handle_send_file(state, {**SID, "path": "big.bin"})

        assert result["bytes_written"] == len(content)
        assert [len(s) for s in written(conn)] == [4096, 4096, 2048]
        assert b"".join(written(conn)) == content

    async def test_the_connections_own_pacing_applies(self, connected_entry, files):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        conn.writer.pacing = Pacing(inter_char_delay_ms=1)
        (files / "x").write_bytes(b"abc")

        with patch.object(threading.Event, "wait", return_value=False):
            await handle_send_file(state, {**SID, "path": "x"})

        assert written(conn) == [b"a", b"b", b"c"]

    async def test_a_file_in_a_subdirectory(self, connected_entry, files):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        (files / "scripts").mkdir()
        (files / "scripts" / "boot.sh").write_bytes(b"echo hi\n")

        result = await handle_send_file(state, {**SID, "path": "scripts/boot.sh"})

        assert result["ok"]
        assert b"".join(written(conn)) == b"echo hi\n"

    async def test_an_empty_file_sends_nothing_and_succeeds(self, connected_entry, files):
        state, conn = connected_entry
        (files / "empty").write_bytes(b"")

        result = await handle_send_file(state, {**SID, "path": "empty"})

        assert (result["ok"], result["bytes_written"]) == (True, 0)

    async def test_the_default_directory_is_files_under_the_spec_root(
        self, connected_entry, tmp_path, monkeypatch
    ):
        monkeypatch.delenv(paths.ENV_FILES_DIR, raising=False)
        monkeypatch.setenv("SERIAL_MCP_SPEC_ROOT", str(tmp_path / "spec"))
        (tmp_path / "spec" / "files").mkdir(parents=True)
        (tmp_path / "spec" / "files" / "hello").write_bytes(b"hi")
        state, conn = connected_entry
        conn.ser.write.side_effect = len

        result = await handle_send_file(state, {**SID, "path": "hello"})

        assert result["ok"]


class TestRefusals:
    @pytest.fixture(autouse=True)
    def secret(self, files, tmp_path):
        (files / "ok.txt").write_bytes(b"fine")
        path = tmp_path / "id_rsa"
        path.write_bytes(b"PRIVATE KEY")
        return path

    @pytest.mark.parametrize(
        "relative",
        ["../id_rsa", "sub/../../id_rsa", "", ".", "sub", "missing.txt", "ok.txt\x00../id_rsa"],
    )
    async def test_bad_paths_are_refused_before_any_byte_is_written(self, connected_entry, files, relative):
        state, conn = connected_entry
        (files / "sub").mkdir(exist_ok=True)

        result = await handle_send_file(state, {**SID, "path": relative})

        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()

    async def test_an_absolute_path_is_refused(self, connected_entry, secret):
        state, conn = connected_entry

        result = await handle_send_file(state, {**SID, "path": str(secret)})

        assert result["error"]["code"] == "invalid_params"
        assert "must be relative" in result["error"]["message"]
        conn.ser.write.assert_not_called()

    @POSIX_ONLY
    async def test_a_symlink_pointing_outside_is_refused(self, connected_entry, files, secret):
        state, conn = connected_entry
        (files / "key").symlink_to(secret)

        result = await handle_send_file(state, {**SID, "path": "key"})

        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()

    @POSIX_ONLY
    async def test_a_fifo_is_refused_without_hanging(self, connected_entry, files):
        state, conn = connected_entry
        os.mkfifo(files / "pipe")

        result = await handle_send_file(state, {**SID, "path": "pipe"})

        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()

    async def test_an_oversize_file_is_refused(self, connected_entry, files):
        state, conn = connected_entry
        (files / "huge").write_bytes(b"x" * (MAX_SEND_FILE_BYTES + 1))

        result = await handle_send_file(state, {**SID, "path": "huge"})

        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()

    async def test_a_paced_file_over_the_duration_cap_is_refused(self, connected_entry, files, monkeypatch):
        state, conn = connected_entry
        monkeypatch.setenv("SERIAL_MCP_MAX_WRITE_SECONDS", "1")
        (files / "slow").write_bytes(b"x" * 200)

        result = await handle_send_file(state, {**SID, "path": "slow", "inter_char_delay_ms": 8})

        assert result["error"]["code"] == "invalid_params"
        assert "1.6 s" in result["error"]["message"]
        conn.ser.write.assert_not_called()

    async def test_an_unpaced_file_is_judged_by_its_time_on_the_wire(self, connected_entry, files):
        state, conn = connected_entry
        (files / "wire").write_bytes(b"x" * MAX_SEND_FILE_BYTES)  # 1 MiB at 115200 baud and 10 bits per byte

        result = await handle_send_file(state, {**SID, "path": "wire"})

        assert result["error"]["code"] == "invalid_params"
        assert "91.0 s" in result["error"]["message"]
        conn.ser.write.assert_not_called()

    @pytest.mark.parametrize("pacing", [{"chunk_size": 3}, {"inter_char_delay_ms": -1}])
    async def test_invalid_pacing_is_refused(self, connected_entry, pacing):
        state, conn = connected_entry

        result = await handle_send_file(state, {**SID, "path": "ok.txt", **pacing})

        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()


class TestFailures:
    async def test_a_write_timeout_reports_the_completed_bytes(self, connected_entry, files):
        state, conn = connected_entry
        (files / "f").write_bytes(b"ABCDEFGH")
        conn.ser.write.side_effect = [2, 2, pyserial.SerialTimeoutException("timeout")]

        with patch.object(threading.Event, "wait", return_value=False):
            result = await handle_send_file(state, {**SID, "path": "f", "chunk_size": 2, "chunk_delay_ms": 1})

        assert result["error"]["code"] == "write_aborted"
        assert result["bytes_written"] == 4

    async def test_a_closed_writer_reports_a_closed_connection(self, connected_entry, files):
        state, conn = connected_entry
        (files / "f").write_bytes(b"x")
        conn.writer.stop()

        result = await handle_send_file(state, {**SID, "path": "f"})

        assert result["error"]["code"] == "connection_closed"


class TestPortLog:
    async def test_the_transfers_tx_entries_sit_between_its_start_and_end_events(
        self, connected_entry, files, tmp_path
    ):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        (files / "env.txt").write_bytes(b"abcde")
        log = PortLog.start(LogRequest(("jsonl",), tmp_path / "logs"), "utf-8")
        conn.log_slot.set(log)

        with patch.object(threading.Event, "wait", return_value=False):
            await handle_send_file(state, {**SID, "path": "env.txt", "chunk_size": 2, "chunk_delay_ms": 1})
        conn.log_slot.close_log()

        entries = read_jsonl(log.files["jsonl"])
        sequence = [e.get("event") or f"tx:{e['text']}" for e in entries]
        assert sequence == ["send_file_start", "tx:ab", "tx:cd", "tx:e", "send_file_end", "close"]
        start, end = entries[0], entries[4]
        assert start["size"] == 5
        assert (end["bytes_written"], end["path"]) == (5, start["path"])

    async def test_a_refused_path_logs_no_events(self, connected_entry, files, tmp_path):
        state, conn = connected_entry
        log = PortLog.start(LogRequest(("jsonl",), tmp_path / "logs"), "utf-8")
        conn.log_slot.set(log)

        await handle_send_file(state, {**SID, "path": "../x"})
        conn.log_slot.close_log()

        assert [e["event"] for e in read_jsonl(log.files["jsonl"])] == ["close"]


class TestCapAndPathReporting:
    async def test_large_chunks_cannot_hide_the_time_the_bytes_take_on_the_wire(
        self, connected_entry, files, monkeypatch
    ):
        state, conn = connected_entry
        conn.baudrate = 9600
        (files / "big").write_bytes(b"x" * 100_000)  # about 104 s on a 9600 baud line

        result = await handle_send_file(
            state, {**SID, "path": "big", "chunk_size": 65536, "chunk_delay_ms": 1}
        )

        assert result["error"]["code"] == "invalid_params"
        assert "104.2 s" in result["error"]["message"]
        conn.ser.write.assert_not_called()

    async def test_a_zero_baud_connection_does_not_crash_the_estimate(self, connected_entry, files):
        state, conn = connected_entry
        conn.baudrate = 0
        conn.ser.write.side_effect = len
        (files / "f").write_bytes(b"abc")

        result = await handle_send_file(state, {**SID, "path": "f"})

        assert result["ok"]

    async def test_the_response_and_log_show_the_path_inside_the_files_directory_only(
        self, connected_entry, files, tmp_path
    ):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        (files / "a").mkdir()
        (files / "a" / "b.txt").write_bytes(b"hi")
        log = PortLog.start(LogRequest(("jsonl",), tmp_path / "logs"), "utf-8")
        conn.log_slot.set(log)

        result = await handle_send_file(state, {**SID, "path": "a/./b.txt"})
        conn.log_slot.close_log()

        logged = [
            e for e in read_jsonl(log.files["jsonl"]) if str(e.get("event", "")).startswith("send_file")
        ]
        assert result["path"] == "a/b.txt"
        assert [e["path"] for e in logged] == ["a/b.txt", "a/b.txt"]
        assert str(files) not in str(result) + str(logged)


class _HeldPort:
    """Port whose first write blocks until released; later writes pass."""

    def __init__(self):
        self.writes: list[bytes] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True
        self.is_open = True

    def write(self, data):
        self.writes.append(data)
        if self._first:
            self._first = False
            self.entered.set()
            self.release.wait(2)
        return len(data)

    def flush(self):
        pass


class TestClaimRecheckedAfterTheFileRead:
    async def test_a_script_claiming_the_connection_during_the_read_stops_the_send(
        self, connected_entry, files
    ):
        state, conn = connected_entry
        (files / "a.txt").write_bytes(b"hello")
        reading = threading.Event()
        proceed = threading.Event()
        real_read = paths.read_confined_file

        def gated_read(*args):
            reading.set()
            assert proceed.wait(5), "the test never released the read"
            return real_read(*args)

        with patch("serial_mcp_server.handlers_file.read_confined_file", side_effect=gated_read):
            task = asyncio.create_task(handle_send_file(state, {**SID, "path": "a.txt"}))
            assert await asyncio.to_thread(reading.wait, 5)
            run, _owner = state.script_jobs.claim("s1", MagicMock())
            assert run is not None
            proceed.set()
            result = await task

        assert result["error"]["code"] == "script_running"
        conn.ser.write.assert_not_called()


class TestCancelledTransfer:
    async def test_the_end_event_follows_the_last_tx_entry_and_counts_the_bytes_sent(
        self, connected_entry, files, tmp_path
    ):
        import asyncio

        from serial_mcp_server.writer import SerialWriter

        state, conn = connected_entry
        port = _HeldPort()
        conn.writer.stop()
        conn.writer.join()
        conn.ser = port
        conn.writer = SerialWriter(port)
        conn.writer.start()
        (files / "long").write_bytes(b"ABCDEFGH")
        log = PortLog.start(LogRequest(("jsonl",), tmp_path / "logs"), "utf-8")
        conn.log_slot.set(log)

        task = asyncio.create_task(
            handle_send_file(state, {**SID, "path": "long", "chunk_size": 2, "chunk_delay_ms": 1000})
        )
        assert await asyncio.to_thread(port.entered.wait, 2)  # the first segment is in the port's write
        task.cancel()
        await asyncio.sleep(0)
        port.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Only the cancellation reaches the job: nothing stops the writer. The transfer's other
        # three segments are a second apart, so the job must end well inside that.
        assert await asyncio.to_thread(writer_goes_idle, conn.writer, "mcp", 0.5)
        conn.log_slot.close_log()

        entries = read_jsonl(log.files["jsonl"])
        sequence = [e.get("event") or f"tx:{e['text']}" for e in entries]
        assert sequence == ["send_file_start", "tx:AB", "send_file_end", "close"]
        assert entries[2]["bytes_written"] == 2
        assert port.writes == [b"AB"]
        await asyncio.to_thread(conn.writer.stop)
        await asyncio.to_thread(conn.writer.join)
