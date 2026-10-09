"""Tests for the adapter that lets the script engine write through a connection's writer."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from serial_mcp_server.connection_session import ConnectionSession
from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.pacing import NO_PACING, Pacing
from serial_mcp_server.port.writer import SerialWriter, WriteAborted

WAIT_S = 5.0


class GatedPort:
    """A port whose first write blocks until released, and which records every write."""

    port = "gated"
    is_open = True

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes: list[bytes] = []
        self._first = True

    def write(self, data: bytes) -> int:
        if self._first:
            self._first = False
            self.entered.set()
            assert self.release.wait(WAIT_S), "the test never released the first write"
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass


@pytest.fixture()
def gated(tmp_path):
    port = GatedPort()
    writer = SerialWriter(port, NO_PACING)
    writer.start()
    conn = SerialConnection(
        connection_id="s1",
        port="gated",
        baudrate=115200,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=0.2,
        write_timeout=0.2,
        encoding="utf-8",
        newline="\n",
        ser=port,
        writer=writer,
        buffer=SerialBuffer(),
    )
    yield port, writer, ConnectionSession(conn, tmp_path)
    port.release.set()
    writer.stop()
    writer.join()


def run_write(session: ConnectionSession, data: bytes, pacing: Pacing | None, cancel: threading.Event):
    """Run ``session.write`` on a thread, as the runner does; return the thread and its outcome."""
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["written"] = session.write(data, pacing, cancel)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


def test_cancelling_a_script_write_queued_behind_another_job_removes_it(gated):
    port, writer, session = gated
    writer.submit(b"PASTE")  # a rw-mirror paste or a serial.write that started before the claim
    assert port.entered.wait(WAIT_S)
    cancel = threading.Event()
    thread, outcome = run_write(session, b"reboot\n", None, cancel)

    cancel.set()
    thread.join(WAIT_S)

    assert not thread.is_alive(), "the runner's write step stayed blocked behind the queue"
    error = outcome.get("error")
    assert isinstance(error, WriteAborted)
    assert error.bytes_written == 0
    port.release.set()
    writer.stop()
    writer.join()
    assert port.writes == [b"PASTE"]  # the cancelled script's bytes never reached the device


def test_a_job_cancelled_before_its_first_write_is_aborted_not_written(gated):
    port, writer, _session = gated
    writer.submit(b"PASTE")
    assert port.entered.wait(WAIT_S)
    job = writer.submit(b"late\n")
    job.cancel.set()  # set while queued, as discard or a stop request does before the job starts

    port.release.set()

    with pytest.raises(WriteAborted) as raised:
        job.future.result(WAIT_S)
    assert raised.value.bytes_written == 0
    assert port.writes == [b"PASTE"]


def test_a_send_file_cancelled_while_queued_logs_zero_bytes_written(gated, tmp_path: Path):
    port, writer, session = gated
    (tmp_path / "blob.bin").write_bytes(b"payload")
    events: list[tuple[str, dict[str, object]]] = []
    session.log_event = lambda name, **fields: events.append((name, fields))  # type: ignore[method-assign]
    writer.submit(b"PASTE")
    assert port.entered.wait(WAIT_S)
    cancel = threading.Event()
    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            session.send_file(tmp_path / "blob.bin", None, cancel)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    cancel.set()
    thread.join(WAIT_S)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), WriteAborted)
    end = [fields for name, fields in events if name == "send_file_end"]
    assert end == [{"path": "blob.bin", "bytes_written": 0}]
    port.release.set()
    writer.stop()
    writer.join()
    assert port.writes == [b"PASTE"]
