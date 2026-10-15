"""Runtime limits exercised through the real runner, session adapter, and serial writer."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import serial as pyserial

from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.pacing import NO_PACING
from serial_mcp_server.port.writer import SerialWriter
from serial_mcp_server.script.connection_session import ConnectionSession
from serial_mcp_server.script.matcher import OutputMatcher
from serial_mcp_server.script.parser import parse
from serial_mcp_server.script.results import RunMetadata, RunState
from serial_mcp_server.script.runner import ScriptRunner

WAIT_SECONDS = 2.0


class RecordingPort:
    """Record accepted segments and optionally hold the first serial call open."""

    port = "recording"
    is_open = True

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.writes: list[bytes] = []
        self.accepted_limit: int | None = None
        self.block_flush = False
        self.write_error: Exception | None = None
        self.flush_error: Exception | None = None

    def write(self, data: bytes) -> int:
        if not self.block_flush:
            self._wait_for_release()
        if self.write_error is not None:
            raise self.write_error
        accepted = len(data) if self.accepted_limit is None else min(len(data), self.accepted_limit)
        if accepted:
            self.writes.append(bytes(data[:accepted]))
        return accepted

    def flush(self) -> None:
        if self.block_flush:
            self._wait_for_release()
        if self.flush_error is not None:
            raise self.flush_error

    def _wait_for_release(self) -> None:
        self.entered.set()
        assert self.release.wait(WAIT_SECONDS), "the test did not release the serial call"


class ObservedSession(ConnectionSession):
    """Replace only reader subscription and event recording at their I/O boundaries."""

    def __init__(self, connection: SerialConnection, files_root: Path) -> None:
        super().__init__(connection, files_root)
        self.matcher: OutputMatcher | None = None
        self.events: list[tuple[str, dict[str, Any]]] = []

    def subscribe(self, matcher: OutputMatcher) -> Callable[[], None]:
        self.matcher = matcher
        return lambda: None

    def log_event(self, name: str, **fields: Any) -> None:
        self.events.append((name, fields))


@pytest.fixture
def managed_session(tmp_path: Path) -> Iterator[tuple[RecordingPort, SerialWriter, ObservedSession]]:
    port = RecordingPort()
    writer = SerialWriter(port, NO_PACING)
    connection = SerialConnection(
        connection_id="s1",
        port="recording",
        baudrate=115200,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=0.2,
        write_timeout=0.2,
        encoding="utf-8",
        newline="\n",
        ser=cast("pyserial.Serial", port),
        writer=writer,
        buffer=SerialBuffer(),
    )
    writer.start()
    try:
        yield port, writer, ObservedSession(connection, tmp_path)
    finally:
        port.release.set()
        writer.stop()
        writer.join()


def make_runner(source: str, session: ObservedSession, tmp_path: Path, max_seconds: float) -> ScriptRunner:
    metadata = RunMetadata("inline", {}, tmp_path, lambda: ())
    return ScriptRunner(
        parse(source, {}, tmp_path), session, metadata, pattern_timeout_seconds=0.01, max_seconds=max_seconds
    )


@pytest.mark.parametrize("send_file", [False, True], ids=["send", "send_file"])
@pytest.mark.parametrize("reason", ["max_duration", "fail_pattern"])
def test_runtime_failure_interrupts_a_paced_write(
    managed_session, tmp_path: Path, send_file: bool, reason: str
):
    port, _writer, session = managed_session
    payload = b"abcdef"
    (tmp_path / "payload.bin").write_bytes(payload)
    command = "@send_file payload.bin" if send_file else "@send abcdef"
    source = f"@fail_on ERROR\n@pacing char 1000ms\n{command}\n@mark should-not-run"
    runner = make_runner(source, session, tmp_path, 0.1 if reason == "max_duration" else 60.0)
    runner.start()
    try:
        assert port.entered.wait(WAIT_SECONDS)
        if reason == "fail_pattern":
            assert session.matcher is not None
            session.matcher.feed(b"ERROR")
        assert runner.done.wait(WAIT_SECONDS), "runtime checks did not interrupt the pacing pause"
        result = runner.result
        assert result is not None
        assert result.state is RunState.FAILED and result.reason == reason
        assert b"".join(port.writes) == payload[:1]
        assert not result.marks
        assert result.steps[-1].outcome == reason
        if send_file:
            ends = [fields for name, fields in session.events if name == "send_file_end"]
            assert ends == [{"path": "payload.bin", "bytes_written": 1}]
    finally:
        runner.cancel()
        runner.join(WAIT_SECONDS)


def test_deadline_removes_a_queued_file_send_without_waiting_for_another_job(managed_session, tmp_path: Path):
    port, writer, session = managed_session
    port.release.clear()
    prior = writer.submit(b"PRIOR")
    assert port.entered.wait(WAIT_SECONDS)
    (tmp_path / "payload.bin").write_bytes(b"payload")
    runner = make_runner("@send_file payload.bin", session, tmp_path, 0.1)
    runner.start()
    try:
        assert runner.done.wait(1.0), "the queued script write ignored its deadline"
        assert runner.result is not None and runner.result.reason == "max_duration"
        assert not port.writes
        ends = [fields for name, fields in session.events if name == "send_file_end"]
        assert ends == [{"path": "payload.bin", "bytes_written": 0}]
        port.release.set()
        assert prior.future.result(WAIT_SECONDS) == len(b"PRIOR")
        assert writer.submit(b"AFTER").future.result(WAIT_SECONDS) == len(b"AFTER")
        assert port.writes == [b"PRIOR", b"AFTER"]
    finally:
        port.release.set()
        runner.cancel()
        runner.join(WAIT_SECONDS)


@pytest.mark.parametrize("accepted_limit", [1, 0], ids=["accepted-segment", "short-write"])
def test_deadline_keeps_the_run_active_until_a_blocked_serial_call_settles(
    managed_session, tmp_path: Path, accepted_limit: int
):
    port, _writer, session = managed_session
    port.accepted_limit = accepted_limit
    port.release.clear()
    (tmp_path / "payload.bin").write_bytes(b"abcdef")
    runner = make_runner("@pacing char 1000ms\n@send_file payload.bin", session, tmp_path, 0.1)
    runner.start()
    try:
        assert port.entered.wait(WAIT_SECONDS)
        assert not runner.done.wait(0.3), "the runner finalized with a serial call still active"
        assert not any(name == "send_file_end" for name, _ in session.events)
        port.release.set()
        assert runner.done.wait(WAIT_SECONDS)
        assert runner.result is not None and runner.result.reason == "max_duration"
        assert port.writes == ([b"a"] if accepted_limit else [])
        ends = [fields for name, fields in session.events if name == "send_file_end"]
        assert ends == [{"path": "payload.bin", "bytes_written": accepted_limit}]
    finally:
        port.release.set()
        runner.cancel()
        runner.join(WAIT_SECONDS)


@pytest.mark.parametrize("reason", ["cancelled", "end_of_stream", "regex_timeout"])
def test_other_terminal_causes_stop_the_write_with_the_original_reason(
    managed_session, tmp_path: Path, reason: str
):
    port, _writer, session = managed_session
    pattern = "(a|aa)+$" if reason == "regex_timeout" else "ERROR"
    runner = make_runner(f"@fail_on {pattern}\n@pacing char 1000ms\n@send abcdef", session, tmp_path, 60.0)
    runner.start()
    try:
        assert port.entered.wait(WAIT_SECONDS)
        assert session.matcher is not None
        if reason == "cancelled":
            runner.cancel()
            session.matcher.feed(b"ERROR")
        elif reason == "end_of_stream":
            session.matcher.close()
        else:
            session.matcher.feed(b"a" * 10000 + b"!")
        assert runner.done.wait(WAIT_SECONDS)
        result = runner.result
        assert result is not None and result.reason == reason
        expected_state = RunState.CANCELLED if reason == "cancelled" else RunState.ERRORED
        assert result.state is expected_state
        assert port.writes == [b"a"]
    finally:
        runner.cancel()
        runner.join(WAIT_SECONDS)


@pytest.mark.parametrize("send_file", [False, True], ids=["send", "send_file"])
@pytest.mark.parametrize("operation", ["write", "flush"])
@pytest.mark.parametrize("error_type", [pyserial.SerialException, OSError])
def test_settlement_errors_do_not_replace_a_detected_deadline(
    managed_session, tmp_path: Path, send_file: bool, operation: str, error_type: type[Exception]
):
    port, _writer, session = managed_session
    port.release.clear()
    error = error_type("serial settlement failed")
    if operation == "write":
        port.write_error = error
    else:
        port.block_flush = True
        port.flush_error = error
    (tmp_path / "payload.bin").write_bytes(b"abcdef")
    command = "@send_file payload.bin" if send_file else "@send abcdef"
    runner = make_runner(f"@pacing char 1000ms\n{command}", session, tmp_path, 0.1)
    runner.start()
    try:
        assert port.entered.wait(WAIT_SECONDS)
        # Synchronize the race: fail the serial call only after the runner has detected its deadline.
        assert runner._stop_event.wait(WAIT_SECONDS)
        port.release.set()
        assert runner.done.wait(WAIT_SECONDS)
        assert runner.result is not None
        assert runner.result.state is RunState.FAILED
        assert runner.result.reason == "max_duration"
    finally:
        port.release.set()
        runner.cancel()
        runner.join(WAIT_SECONDS)
