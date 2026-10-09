"""Behavioral tests for the synchronous script engine and result files."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import serial as pyserial

from serial_mcp_server import config
from serial_mcp_server.config import DEFAULT_SCRIPT_MAX_SECONDS, ConfigError
from serial_mcp_server.port.pacing import NO_PACING, Pacing
from serial_mcp_server.port.writer import WriteAborted, WriterClosed
from serial_mcp_server.script import Script, parse
from serial_mcp_server.script import runner as script_runner
from serial_mcp_server.script.matcher import OutputMatcher
from serial_mcp_server.script.runner import (
    PATTERN_TIMEOUT_SECONDS,
    RunMetadata,
    RunResult,
    RunState,
    RunStatus,
    ScriptRunner,
)

EXAMPLE_SCRIPT = r"""# U-Boot smoke test
@timeout 10s
@prompt --off
@fail_on "(?i)error|panic"

version
@expect "U-Boot 20\d\d"
@expect "=> "
@prompt "=> "
printenv bootcmd
setenv serverip {{SERVER_IP}}
@mark tftp
@prompt --off
tftpboot ${loadaddr} {{IMAGE}}
@expect "Bytes transferred" --timeout 60s
@expect "=> "
"""

EXAMPLE_RESPONSES = {
    b"version\n": b"U-Boot 2026.10\r\n=> ",
    b"printenv bootcmd\n": b"bootcmd=run boot\r\n=> ",
    b"setenv serverip 192.0.2.1\n": b"=> ",
    b"tftpboot ${loadaddr} fitImage\n": b"Bytes transferred = 32\r\n=> ",
}


class FakePortSession:
    newline = "\n"
    encoding = "utf-8"

    def __init__(
        self,
        responses: Mapping[bytes, bytes] | None = None,
        *,
        echo: bool = True,
        block_write: bool = False,
        write_error: Exception | None = None,
        accepted_bytes: int | None = None,
        close_on_subscribe: bool = False,
    ) -> None:
        self.responses = dict(responses or {})
        self.echo = echo
        self.block_write = block_write
        self.write_error = write_error
        self.accepted_bytes = accepted_bytes
        self.close_on_subscribe = close_on_subscribe
        self.matcher: OutputMatcher | None = None
        self.subscribed = threading.Event()
        self.unsubscribed = threading.Event()
        self.wait_entered = threading.Event()
        self.write_started = threading.Event()
        self.writes: list[tuple[bytes, Pacing | None]] = []
        self.lines: list[tuple[str, bool]] = []
        self.events: list[tuple[str, dict[str, object]]] = []
        self._log_paths: tuple[Path, ...] = ()

    def subscribe(self, matcher: OutputMatcher):
        self.matcher = matcher
        self.subscribed.set()
        wait = matcher.wait

        def observed_wait(deadline: float) -> bool:
            self.wait_entered.set()
            return wait(deadline)

        matcher.wait = observed_wait
        if self.close_on_subscribe:
            matcher.close()

        def unsubscribe() -> None:
            self.unsubscribed.set()

        return unsubscribe

    def write(self, data: bytes, pacing: Pacing | None, cancel: threading.Event) -> int:
        self.write_started.set()
        if self.block_write:
            if cancel.wait(timeout=5.0):
                raise WriteAborted("fake write cancelled", 0)
            raise TimeoutError("fake write did not receive cancellation")
        if cancel.is_set():
            raise WriteAborted("fake write cancelled", 0)
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((data, pacing))
        if self.matcher is None:
            raise RuntimeError("runner wrote before subscribing to output")
        written = len(data) if self.accepted_bytes is None else min(self.accepted_bytes, len(data))
        if self.echo and written:
            self.matcher.feed(data[:written])
        if written == len(data):
            response = self.responses.get(data)
            if response is not None:
                self.matcher.feed(response)
        return written

    def send_file(self, path: Path, pacing: Pacing | None, cancel: threading.Event) -> int:
        return self.write(path.read_bytes(), pacing, cancel)

    def set_line(self, line: str, value: bool) -> None:
        self.lines.append((line, value))

    def log_event(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))

    def feed(self, data: bytes) -> None:
        if self.matcher is None:
            raise RuntimeError("session has no output subscriber")
        self.matcher.feed(data)

    def log_paths(self) -> tuple[Path, ...]:
        return self._log_paths

    def set_log_paths(self, paths: tuple[Path, ...]) -> None:
        self._log_paths = paths


def make_runner(
    source: str,
    session: FakePortSession,
    result_directory: Path,
    *,
    variables: Mapping[str, str] | None = None,
    script_name: str = "inline",
    files_directory: Path | None = None,
    result_path: Path | None = None,
    pattern_timeout_seconds: float = PATTERN_TIMEOUT_SECONDS,
    max_seconds: float = DEFAULT_SCRIPT_MAX_SECONDS,
) -> tuple[ScriptRunner, Script]:
    parsed = parse(source, variables or {}, files_directory or result_directory)
    metadata = RunMetadata(
        script_name=script_name,
        connection_config={
            "port": "mock",
            "baudrate": 115200,
            "encoding": session.encoding,
            "newline": session.newline,
        },
        result_directory=result_directory,
        log_paths=session.log_paths,
        result_path=result_path,
    )
    return ScriptRunner(parsed, session, metadata, pattern_timeout_seconds, max_seconds=max_seconds), parsed


def test_design_example_runs_to_passed_with_echoing_device_responses(tmp_path: Path) -> None:
    session = FakePortSession(EXAMPLE_RESPONSES)
    runner, script = make_runner(
        EXAMPLE_SCRIPT,
        session,
        tmp_path / "logs",
        variables={"SERVER_IP": "192.0.2.1", "IMAGE": "fitImage"},
        script_name="uboot-smoke.serial",
    )
    runner.start()

    assert runner.done.wait(timeout=2.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.PASSED
    assert len(result.steps) == len(script.steps)
    assert all(step.outcome == "passed" for step in result.steps)
    assert session.writes == [
        (b"version\n", None),
        (b"printenv bootcmd\n", None),
        (b"setenv serverip 192.0.2.1\n", None),
        (b"tftpboot ${loadaddr} fitImage\n", None),
    ]
    serverip_step = next(step for step in result.steps if "{{SERVER_IP}}" in step.text)
    assert serverip_step.text == "setenv serverip {{SERVER_IP}}"
    assert session.subscribed.is_set()
    assert session.unsubscribed.is_set()


def test_result_file_keeps_metadata_and_numbers_name_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_start = datetime(2026, 10, 8, 14, 5, tzinfo=UTC)
    local_start = fixed_start.astimezone()
    base = f"serial-mcp-script-{local_start:%d%m%Y-%H%M}"
    result_directory = tmp_path / "logs"
    result_directory.mkdir()
    (result_directory / f"{base}.json").write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(script_runner, "_utc_now", lambda: fixed_start)
    log_path = result_directory / "serial-mcp-log.txt"
    session = FakePortSession()
    session.set_log_paths((log_path,))
    runner, _ = make_runner(
        "# smoke\n@mark done\n@send {{TOKEN}}",
        session,
        result_directory,
        variables={"TOKEN": "secret-value"},
        script_name="smoke.serial",
    )

    runner.start()

    assert runner.done.wait(timeout=2.0)

    result = runner.result
    result_path = runner.result_path
    assert result is not None
    assert result_path is not None
    assert result.state is RunState.PASSED
    assert result_path == result_directory / f"{base}-1.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["state"] == "passed"
    assert payload["script"] == "smoke.serial"
    assert payload["variables"] == ["TOKEN"]
    assert payload["steps"][1]["text"] == "@send {{TOKEN}}"
    assert payload["connection"]["baudrate"] == 115200
    assert payload["log_paths"] == [str(log_path)]
    assert payload["marks"] == ["done"]
    if os.name != "nt":
        assert stat.S_IMODE(result_path.stat().st_mode) & 0o077 == 0


def test_first_result_file_uses_unnumbered_base_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_start = datetime(2026, 10, 8, 14, 5, tzinfo=UTC)
    local_start = fixed_start.astimezone()
    expected_path = tmp_path / "logs" / f"serial-mcp-script-{local_start:%d%m%Y-%H%M}.json"
    monkeypatch.setattr(script_runner, "_utc_now", lambda: fixed_start)
    runner, _ = make_runner("@mark done", FakePortSession(), expected_path.parent)

    runner.start()

    assert runner.done.wait(timeout=2.0)

    assert runner.result_path == expected_path
    assert json.loads(expected_path.read_text(encoding="utf-8"))["state"] == "passed"


def test_explicit_result_path_is_written_verbatim(tmp_path: Path) -> None:
    result_path = tmp_path / "reports" / "custom-run.json"
    runner, _ = make_runner(
        "@mark done",
        FakePortSession(),
        tmp_path / "logs",
        result_path=result_path,
    )

    runner.start()

    assert runner.done.wait(timeout=2.0)

    assert runner.result_path == result_path
    assert json.loads(result_path.read_text(encoding="utf-8"))["state"] == "passed"


def test_missing_expect_fails_at_its_line_with_trailing_output(tmp_path: Path) -> None:
    session = FakePortSession()
    runner, _ = make_runner(
        "@prompt --off\nversion\n@expect missing --timeout 10ms",
        session,
        tmp_path,
    )
    runner.start()

    assert runner.done.wait(timeout=2.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.FAILED
    assert result.reason == "timeout"
    assert result.failure is not None
    assert result.failure.line == 3
    assert result.failure.text == "@expect missing --timeout 10ms"
    assert result.failure.output_tail.endswith("version\n")
    result_path = runner.result_path
    assert result_path is not None
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["state"] == "failed"
    assert payload["reason"] == "timeout"
    assert payload["steps"][-1]["outcome"] == "timeout"
    assert payload["failure"]["line"] == 3
    assert payload["failure"]["output_tail"].endswith("version\n")


def test_timeout_directive_bounds_prompt_wait_and_stops_following_steps(tmp_path: Path) -> None:
    session = FakePortSession()
    runner, _ = make_runner(
        "@timeout 10ms\n@prompt READY\nboot\n@send never",
        session,
        tmp_path,
    )
    runner.start()

    finished = runner.done.wait(timeout=2.0)
    if not finished:
        runner.cancel()
        _ = runner.done.wait(timeout=1.0)
    assert finished

    result = runner.result
    assert result is not None
    assert result.state is RunState.FAILED
    assert result.reason == "timeout"
    assert result.failure is not None
    assert result.failure.line == 3
    assert session.writes == [(b"boot\n", None)]


@pytest.mark.parametrize("command", ["boot", "@sendline boot"])
def test_prompt_waits_before_running_the_next_step(command: str, tmp_path: Path) -> None:
    session = FakePortSession()
    runner, _ = make_runner(f"@prompt READY\n{command}\n@send next", session, tmp_path)
    runner.start()

    assert session.wait_entered.wait(timeout=1.0)
    assert session.writes == [(b"boot\n", None)]
    session.feed(b"READY")
    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.PASSED
    assert session.writes == [(b"boot\n", None), (b"next", None)]


def test_failure_output_tail_is_limited_to_two_kibibytes(tmp_path: Path) -> None:
    response = b"x" * 4096
    session = FakePortSession({b"version\n": response})
    runner, _ = make_runner(
        "@prompt --off\nversion\n@expect missing --timeout 10ms",
        session,
        tmp_path,
    )
    runner.start()

    assert runner.done.wait(timeout=2.0)

    result = runner.result
    assert result is not None
    assert result.failure is not None
    assert result.failure.output_tail == "x" * 2048


def test_fail_pattern_interrupts_a_long_sleep(tmp_path: Path) -> None:
    session = FakePortSession()
    runner, _ = make_runner("@fail_on PANIC\n@mark sleeping\n@sleep 60s", session, tmp_path)
    runner.start()

    assert session.wait_entered.wait(timeout=1.0)
    session.feed(b"PANIC")
    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.FAILED
    assert result.reason == "fail_pattern"
    assert result.failure is not None
    assert result.failure.matched_text == "PANIC"


def test_fail_pattern_spanning_output_chunks_stops_remaining_steps(tmp_path: Path) -> None:
    session = FakePortSession()
    runner, _ = make_runner(
        "@fail_on PANIC\n@sleep 60s\n@send never",
        session,
        tmp_path,
    )
    runner.start()

    assert session.wait_entered.wait(timeout=1.0)
    session.wait_entered.clear()
    session.feed(b"PAN")
    assert session.wait_entered.wait(timeout=1.0)
    session.feed(b"IC")
    finished = runner.done.wait(timeout=1.0)
    if not finished:
        runner.cancel()
        _ = runner.done.wait(timeout=1.0)
    assert finished

    result = runner.result
    assert result is not None
    assert result.state is RunState.FAILED
    assert result.reason == "fail_pattern"
    assert result.failure is not None
    assert result.failure.line == 2
    assert result.failure.matched_text == "PANIC"
    assert session.writes == []


def test_sleep_completes_when_its_duration_elapses(tmp_path: Path) -> None:
    runner, _ = make_runner("@sleep 1ms\n@mark done", FakePortSession(), tmp_path)
    runner.start()

    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.PASSED
    assert result.marks == ("done",)


@pytest.mark.parametrize("source", ["@expect never --timeout 60s", "@sleep 60s"])
def test_cancel_interrupts_expect_and_sleep(source: str, tmp_path: Path) -> None:
    session = FakePortSession()
    runner, _ = make_runner(source, session, tmp_path)
    runner.start()

    assert session.wait_entered.wait(timeout=1.0)
    runner.cancel()
    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.CANCELLED


def test_cancel_during_pulse_restores_control_line(tmp_path: Path) -> None:
    session = FakePortSession()
    runner, _ = make_runner("@pulse dtr 60s", session, tmp_path)
    runner.start()

    assert session.wait_entered.wait(timeout=1.0)
    runner.cancel()
    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.CANCELLED
    assert result.failure is not None
    assert result.failure.line == 1
    assert session.lines == [("dtr", False), ("dtr", True)]


def test_cancel_interrupts_a_paced_write_and_snapshot_does_not_block(tmp_path: Path) -> None:
    session = FakePortSession(block_write=True)
    runner, _ = make_runner("@pacing char 1ms\n@sendline wait", session, tmp_path)
    runner.start()
    assert session.write_started.wait(timeout=1.0)

    status_ready = threading.Event()
    statuses: list[RunStatus] = []

    def read_status() -> None:
        statuses.append(runner.snapshot())
        status_ready.set()

    status_thread = threading.Thread(target=read_status, daemon=True)
    status_thread.start()
    assert status_ready.wait(timeout=1.0)
    runner.cancel()
    assert runner.done.wait(timeout=1.0)
    status_thread.join(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.CANCELLED
    assert statuses[0].state is RunState.RUNNING
    assert statuses[0].current_line == 2


def test_snapshot_shows_source_text_and_variable_names(tmp_path: Path) -> None:
    session = FakePortSession(block_write=True)
    runner, _ = make_runner(
        "@send {{TOKEN}}",
        session,
        tmp_path,
        variables={"TOKEN": "secret-value"},
    )
    runner.start()
    assert session.write_started.wait(timeout=1.0)

    status = runner.snapshot()

    assert status.state is RunState.RUNNING
    assert status.current_line == 1
    assert status.current_text == "@send {{TOKEN}}"
    assert status.variables == ("TOKEN",)

    runner.cancel()
    assert runner.done.wait(timeout=1.0)


@pytest.mark.parametrize(
    ("write_error", "expected_state"),
    [
        (pyserial.SerialException("device lost"), RunState.ERRORED),
        (WriterClosed("connection closed"), RunState.CANCELLED),
    ],
)
def test_write_errors_set_the_planned_terminal_state(
    write_error: Exception, expected_state: RunState, tmp_path: Path
) -> None:
    session = FakePortSession(write_error=write_error)
    runner, _ = make_runner("@send ping", session, tmp_path)
    runner.start()

    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is expected_state


def test_partial_write_ends_the_run_as_errored(tmp_path: Path) -> None:
    runner, _ = make_runner("@send ping", FakePortSession(accepted_bytes=1), tmp_path)
    runner.start()

    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.ERRORED
    assert result.reason == "serial_error"
    assert result.failure is not None
    assert result.failure.detail is not None
    assert "1" in result.failure.detail
    assert "4" in result.failure.detail


def test_partial_file_send_ends_the_run_as_errored(tmp_path: Path) -> None:
    files_directory = tmp_path / "files"
    files_directory.mkdir()
    (files_directory / "image.bin").write_bytes(b"image")
    runner, _ = make_runner(
        "@send_file image.bin",
        FakePortSession(accepted_bytes=1),
        tmp_path / "results",
        files_directory=files_directory,
    )
    runner.start()

    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.ERRORED
    assert result.reason == "serial_error"


def test_closed_matcher_ends_the_run_as_errored(tmp_path: Path) -> None:
    session = FakePortSession(close_on_subscribe=True)
    runner, _ = make_runner("@expect ready --timeout 1s", session, tmp_path)
    runner.start()

    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.ERRORED
    assert result.reason == "end_of_stream"


def test_maximum_duration_fails_a_long_sleep(tmp_path: Path) -> None:
    session = FakePortSession()
    runner, _ = make_runner("@sleep 60s", session, tmp_path, max_seconds=0.02)
    runner.start()

    assert runner.done.wait(timeout=1.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.FAILED
    assert result.reason == "max_duration"


def test_invalid_maximum_duration_is_rejected() -> None:
    with pytest.raises(ConfigError, match="SERIAL_MCP_SCRIPT_MAX_SECONDS"):
        config.load({"SERIAL_MCP_SCRIPT_MAX_SECONDS": "nan"})


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_maximum_duration_means_the_default(blank: str) -> None:
    settings = config.load({"SERIAL_MCP_SCRIPT_MAX_SECONDS": blank})

    assert settings.script_max_seconds == DEFAULT_SCRIPT_MAX_SECONDS


def test_failed_thread_start_keeps_the_runner_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = make_runner("@mark done", FakePortSession(), tmp_path)
    original_start = threading.Thread.start

    def refuse_to_start(thread: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse_to_start)
    with pytest.raises(RuntimeError, match="start new thread"):
        runner.start()

    monkeypatch.setattr(threading.Thread, "start", original_start)
    runner.cancel()
    runner.join(timeout=0.0)

    runner.start()
    assert runner.done.wait(timeout=2.0)
    result = runner.result
    assert result is not None
    assert result.state is RunState.CANCELLED


def test_script_runner_import_does_not_load_mcp():
    command = (
        "import sys; import serial_mcp_server.script.runner; "
        "assert 'mcp' not in sys.modules, 'script runner imported mcp'"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter command
        [sys.executable, "-I", "-c", command],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_control_directives_reach_the_port_session(tmp_path: Path) -> None:
    files = tmp_path / "files"
    files.mkdir()
    (files / "image.bin").write_bytes(b"image")
    source = r"""@prompt --off
@pacing char 1ms
@send hello
@newline \r\n
@sendline world
@sendhex 00 ff
@dtr off
@rts on
@pulse dtr 1ms
@pacing off
@mark boot stage
@send_file image.bin
@fail_on PANIC
@fail_on --clear
@send PANIC
@discard
"""
    session = FakePortSession()
    runner, _ = make_runner(source, session, tmp_path / "results", files_directory=files)
    runner.start()

    assert runner.done.wait(timeout=2.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.PASSED
    assert [data for data, _ in session.writes] == [
        b"hello",
        b"world\r\n",
        b"\x00\xff",
        b"image",
        b"PANIC",
    ]
    assert session.writes[0][1] == Pacing(inter_char_delay_ms=1)
    assert session.writes[3][1] == NO_PACING
    assert session.lines == [("dtr", False), ("rts", True), ("dtr", False), ("dtr", True)]
    assert ("mark", {"label": "boot stage"}) in session.events
    assert session.matcher is not None
    assert session.matcher.snapshot().text == ""


# Alternation that overlaps itself: both re and regex backtrack exponentially on this text, so a search
# with a half-second limit always times out, and a search without one would hang the test.
CATASTROPHIC_PATTERN = r"(a|aa)+$"
CATASTROPHIC_TEXT = "a" * 60 + "b"
CATASTROPHIC_OUTPUT = CATASTROPHIC_TEXT.encode()
LIMIT_SECONDS = 0.5


def timeout_detail(line: int, limit: float = LIMIT_SECONDS) -> str:
    return (
        f"The pattern on line {line} ran longer than {limit:g} s, so the run stopped. "
        "Rewrite it without nested quantifiers."
    )


def run_to_end(
    source: str,
    session: FakePortSession,
    tmp_path: Path,
    *,
    variables: Mapping[str, str] | None = None,
) -> RunResult:
    runner, _ = make_runner(
        source, session, tmp_path, variables=variables, pattern_timeout_seconds=LIMIT_SECONDS
    )
    runner.start()
    assert runner.done.wait(timeout=10.0)
    result = runner.result
    assert result is not None
    return result


def test_catastrophic_expect_ends_the_run_errored_and_keeps_earlier_steps(tmp_path: Path) -> None:
    session = FakePortSession({b"go\n": CATASTROPHIC_OUTPUT}, echo=False)
    source = f'@mark before\ngo\n@expect "{CATASTROPHIC_PATTERN}" --timeout 30s'

    result = run_to_end(source, session, tmp_path)

    assert result.state is RunState.ERRORED
    assert result.reason == "regex_timeout"
    assert [step.outcome for step in result.steps] == ["passed", "passed", "regex_timeout"]
    assert result.marks == ("before",)
    failure = result.failure
    assert failure is not None
    assert failure.reason == "regex_timeout"
    assert failure.line == 3
    assert failure.output_tail == CATASTROPHIC_TEXT
    assert failure.detail == timeout_detail(3)


@pytest.mark.parametrize(
    ("source", "pattern_line", "running_line", "outcomes"),
    [
        pytest.param(
            f'@mark before\n@fail_on "{CATASTROPHIC_PATTERN}"\n@sleep 60s',
            2,
            3,
            ["passed", "passed", "regex_timeout"],
            id="fail-on-then-sleep",
        ),
        pytest.param(
            f'@fail_on "{CATASTROPHIC_PATTERN}"\n@expect never --timeout 30s',
            1,
            2,
            ["passed", "regex_timeout"],
            id="fail-on-then-expect",
        ),
    ],
)
def test_catastrophic_fail_on_pattern_ends_the_run_errored_and_names_its_own_line(
    source: str,
    pattern_line: int,
    running_line: int,
    outcomes: list[str],
    tmp_path: Path,
) -> None:
    session = FakePortSession()
    runner, _ = make_runner(source, session, tmp_path, pattern_timeout_seconds=LIMIT_SECONDS)
    runner.start()
    assert session.wait_entered.wait(timeout=5.0)

    session.feed(CATASTROPHIC_OUTPUT)
    assert runner.done.wait(timeout=10.0)

    result = runner.result
    assert result is not None
    assert result.state is RunState.ERRORED
    assert result.reason == "regex_timeout"
    assert [step.outcome for step in result.steps] == outcomes
    failure = result.failure
    assert failure is not None
    assert failure.line == running_line
    assert failure.detail == timeout_detail(pattern_line)


def test_catastrophic_prompt_pattern_names_the_prompt_line_not_the_step_waiting_for_it(
    tmp_path: Path,
) -> None:
    session = FakePortSession({b"go\n": CATASTROPHIC_OUTPUT}, echo=False)

    result = run_to_end(f'@prompt "{CATASTROPHIC_PATTERN}"\ngo', session, tmp_path)

    assert result.state is RunState.ERRORED
    assert result.reason == "regex_timeout"
    failure = result.failure
    assert failure is not None
    assert failure.line == 2
    assert failure.detail == timeout_detail(1)


def test_regex_timeout_result_never_repeats_a_variable_value(tmp_path: Path) -> None:
    session = FakePortSession({b"go\n": CATASTROPHIC_OUTPUT}, echo=False)
    source = 'go\n@expect "(a|aa)+(?:{{TOKEN}})?$" --timeout 30s'

    result = run_to_end(source, session, tmp_path, variables={"TOKEN": "hunter2-s3cret"})

    assert result.reason == "regex_timeout"
    serialized = json.dumps(result.to_json())
    assert "hunter2" not in serialized
    assert "s3cret" not in serialized
    assert result.failure is not None
    assert result.failure.detail == timeout_detail(2)


def test_catastrophic_search_leaves_other_threads_running_and_cancel_wins(tmp_path: Path) -> None:
    session = FakePortSession({b"go\n": CATASTROPHIC_OUTPUT}, echo=False)
    source = f'go\n@expect "{CATASTROPHIC_PATTERN}" --timeout 60s'
    runner, _ = make_runner(source, session, tmp_path, pattern_timeout_seconds=2.0)
    runner.start()
    while runner.snapshot().current_line != 2 and not runner.done.is_set():
        time.sleep(0.005)
    time.sleep(0.2)  # let the runner get into the search

    # Holding the interpreter lock for the search would stall this thread until the search ended.
    started = time.monotonic()
    for _ in range(10):
        time.sleep(0.01)
    assert time.monotonic() - started < 0.5
    assert not runner.done.is_set()

    runner.cancel()

    assert runner.done.wait(timeout=5.0)
    result = runner.result
    assert result is not None
    assert result.state is RunState.CANCELLED  # the cancel outranks the timeout that ended the search
    assert result.reason == "cancelled"


def test_parser_and_runner_accept_the_same_pattern_syntax(tmp_path: Path) -> None:
    session = FakePortSession({b"go\n": b"value: 42\n"}, echo=False)
    # \p{N} is a Unicode property class: regex accepts it, re rejects it.
    source = 'go\n@expect "value: \\p{N}+"'

    result = run_to_end(source, session, tmp_path)

    assert result.state is RunState.PASSED
    assert result.steps[-1].matched_text == "value: 42"
