"""Tests for the synchronous port stack opener shared by serial.open and the script command."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from serial_mcp_server.port import mirror
from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.connection import stop_connections
from serial_mcp_server.port.pacing import NO_PACING
from serial_mcp_server.port.reader import ReaderThread
from serial_mcp_server.port.stack import PortLogStartError, conn_config, open_port_stack
from serial_mcp_server.portlog import LogRequest
from tests.helpers import read_jsonl


def _threads(name: str) -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == name and t.is_alive()}


def open_stack(ser: MagicMock, log_directory: Path | None):
    log_request = LogRequest(("jsonl",), log_directory) if log_directory is not None else None
    return open_port_stack(
        ser,
        port="/dev/ttyUSB0",
        baudrate=115200,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=0.2,
        write_timeout=0.2,
        encoding="utf-8",
        newline="\r\n",
        pacing=NO_PACING,
        log_request=log_request,
        connection_id="s1",
        mirror_mode="off",
        mirror_link=None,
    )


def only_log_events(directory: Path) -> list[dict[str, Any]]:
    (path,) = directory.iterdir()
    return read_jsonl(path)


def test_open_returns_a_started_connection_whose_log_begins_with_the_open_event(mock_serial, tmp_path):
    before_writers = _threads("serial-writer")
    conn = open_stack(mock_serial, tmp_path)
    try:
        assert conn.connection_id == "s1"
        assert conn.ser is mock_serial
        assert conn.reader is not None
        assert _threads("serial-writer") - before_writers
        assert conn.log_slot.log is not None
    finally:
        stop_connections([conn])

    events = only_log_events(tmp_path)
    assert events[0]["event"] == "open"
    assert events[0]["config"] == conn_config(conn)


def test_open_without_a_log_request_starts_no_log(mock_serial):
    conn = open_stack(mock_serial, None)
    try:
        assert conn.log_slot.log is None
    finally:
        stop_connections([conn])


def test_unwritable_log_directory_raises_a_log_start_error_and_starts_nothing(mock_serial, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    before = _threads("serial-writer") | _threads("serial-reader")

    with pytest.raises(PortLogStartError) as raised:
        open_stack(mock_serial, blocker / "logs")

    assert raised.value.directory == blocker / "logs"
    assert (_threads("serial-writer") | _threads("serial-reader")) <= before


def test_reader_creation_failure_closes_the_log(mock_serial, tmp_path):
    before = _threads("serial-portlog")
    with (
        patch("serial_mcp_server.port.stack.create_reader", side_effect=OSError("no ptys")),
        pytest.raises(OSError, match="no ptys"),
    ):
        open_stack(mock_serial, tmp_path)

    assert [event["event"] for event in only_log_events(tmp_path)] == ["close"]
    assert _threads("serial-portlog") <= before


def test_reader_start_failure_stops_the_writer_and_closes_the_log(mock_serial, tmp_path):
    reader = MagicMock()
    reader.start.side_effect = RuntimeError("cannot start reader")
    before_writers = _threads("serial-writer")
    before_logs = _threads("serial-portlog")
    with (
        patch("serial_mcp_server.port.stack.create_reader", return_value=reader),
        pytest.raises(RuntimeError, match="cannot start reader"),
    ):
        open_stack(mock_serial, tmp_path)

    reader.signal_stop.assert_called_once()
    reader.join.assert_called_once()
    assert _threads("serial-writer") <= before_writers
    assert _threads("serial-portlog") <= before_logs
    assert [event["event"] for event in only_log_events(tmp_path)] == ["open", "close"]


def test_writer_start_failure_releases_the_log_and_never_starts_the_reader(mock_serial, tmp_path):
    reader = MagicMock()
    before_logs = _threads("serial-portlog")
    with (
        patch("serial_mcp_server.port.stack.create_reader", return_value=reader),
        patch(
            "serial_mcp_server.port.stack.SerialWriter.start", side_effect=RuntimeError("cannot start writer")
        ),
        pytest.raises(RuntimeError, match="cannot start writer"),
    ):
        open_stack(mock_serial, tmp_path)

    reader.start.assert_not_called()
    assert _threads("serial-portlog") <= before_logs


@pytest.mark.parametrize("failing_thread", ["serial-reader", "serial-writer"])
def test_a_thread_that_cannot_start_does_not_mask_the_error_or_skip_the_log_close(
    mock_serial, tmp_path, failing_thread
):
    """Both threads assign their handle before ``Thread.start``, so a failed start leaves an unjoinable one."""
    real_start = threading.Thread.start

    def start(self: threading.Thread) -> None:
        if self.name == failing_thread:
            raise RuntimeError("can't start new thread")
        real_start(self)

    before_logs = _threads("serial-portlog")
    before_writers = _threads("serial-writer")
    with (
        patch(
            "serial_mcp_server.port.stack.create_reader",
            return_value=ReaderThread(mock_serial, SerialBuffer()),
        ),
        patch.object(threading.Thread, "start", start),
        pytest.raises(RuntimeError, match="can't start new thread"),
    ):
        open_stack(mock_serial, tmp_path)

    assert [event["event"] for event in only_log_events(tmp_path)][-1] == "close"
    assert _threads("serial-portlog") <= before_logs
    assert _threads("serial-writer") <= before_writers


@pytest.mark.skipif(os.name == "nt", reason="the PTY mirror is Unix-only")
def test_a_mirror_reader_that_cannot_start_releases_its_pty_symlink_and_index(mock_serial, tmp_path):
    mock_serial.baudrate = 115200
    link_base = str(tmp_path / "mirror")
    real_start = threading.Thread.start

    def start(self: threading.Thread) -> None:
        if self.name == "serial-reader":
            raise RuntimeError("can't start new thread")
        real_start(self)

    with patch.object(threading.Thread, "start", start), pytest.raises(RuntimeError, match="can't start"):
        open_port_stack(
            mock_serial,
            port="/dev/ttyUSB0",
            baudrate=115200,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.2,
            write_timeout=0.2,
            encoding="utf-8",
            newline="\r\n",
            pacing=NO_PACING,
            log_request=None,
            connection_id="s1",
            mirror_mode="ro",
            mirror_link=link_base,
        )

    assert not os.path.lexists(f"{link_base}0")
    assert mirror._mirror_indices_in_use == set()
