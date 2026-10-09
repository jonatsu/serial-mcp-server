"""Tests for the ``serial_mcp_script`` command.

Most run the command in a subprocess against a PTY device. A few call its internals in process: the
log-start failure, an unexpected error, and the Ctrl-C retry loops.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import serial as pyserial

from serial_mcp_server import script_cli
from serial_mcp_server.portlog import LogRequest
from tests.helpers import read_jsonl

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY and POSIX signals are Unix-only")

if sys.platform != "win32":
    import fcntl
    import select
    import tty

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130
CLI = [sys.executable, "-m", "serial_mcp_server.script_cli"]
# A child that inherits SIGINT as ignored (a background job) never sees Ctrl-C, so restore Python's handler.
SIGINT_CLI = [
    sys.executable,
    "-c",
    "import signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); "
    "from serial_mcp_server.script_cli import main; sys.exit(main())",
]
PROCESS_TIMEOUT_SECONDS = 30


@dataclass
class DemoDevice:
    """A PTY-backed device that answers each received line with a reply and a prompt."""

    path: str
    lines: list[str] = field(default_factory=list)


@pytest.fixture()
def device() -> Iterator[DemoDevice]:
    master, slave = os.openpty()
    tty.setraw(slave)  # a real UART does not echo; neither does the demo device
    demo = DemoDevice(path=os.ttyname(slave))
    stop = threading.Event()

    def serve() -> None:
        pending = b""
        while not stop.is_set():
            ready, _, _ = select.select([master], [], [], 0.05)
            if not ready:
                continue
            try:
                pending += os.read(master, 1024)
            except OSError:
                return
            while b"\r\n" in pending:
                raw, pending = pending.split(b"\r\n", 1)
                line = raw.decode()
                demo.lines.append(line)
                os.write(master, f"reply:{line}\r\n> ".encode())

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    yield demo
    stop.set()
    server.join()
    os.close(master)
    os.close(slave)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Keep the developer's ``SERIAL_MCP_*`` settings out of the child processes."""
    for name in [name for name in os.environ if name.startswith("SERIAL_MCP_")]:
        monkeypatch.delenv(name)


def write_script(directory: Path, source: str) -> Path:
    path = directory / "case.serial"
    path.write_text(source, encoding="utf-8")
    return path


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [*CLI, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def read_result(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


PASSING = """# ping the device
@timeout 5s
@sendline ping {{SERVER_IP}}
@expect "reply:ping {{SERVER_IP}}"
"""

PING_SCRIPT = "@timeout 5s\n@sendline ping\n@expect reply:ping\n"


def test_passing_script_exits_zero_and_writes_the_result(device, tmp_path):
    script = write_script(tmp_path, PASSING)
    result_path = tmp_path / "out" / "result.json"

    done = run_cli(
        script,
        "--port",
        device.path,
        "--var",
        "SERVER_IP=10.0.0.1",
        "--result",
        result_path,
        "--log-dir",
        tmp_path / "logs",
    )

    assert done.returncode == EXIT_PASSED, done.stderr
    assert done.stdout == ""
    assert "Traceback" not in done.stderr
    result = read_result(result_path)
    assert result["schema_version"] == 1
    assert result["state"] == "passed"
    assert [step["line"] for step in result["steps"]] == [2, 3, 4]
    assert result["variables"] == ["SERVER_IP"]
    assert str(result_path) in done.stderr


def test_var_values_reach_the_device_but_only_names_reach_the_result(device, tmp_path):
    script = write_script(tmp_path, PASSING)
    result_path = tmp_path / "result.json"

    done = run_cli(
        script,
        "--port",
        device.path,
        "--var",
        "SERVER_IP=10.0.0.1",
        "--result",
        result_path,
        "--log-dir",
        tmp_path / "logs",
    )

    assert done.returncode == EXIT_PASSED, done.stderr
    assert device.lines == ["ping 10.0.0.1"]
    result = read_result(result_path)
    steps = result["steps"]
    assert steps[1]["text"] == "@sendline ping {{SERVER_IP}}"
    assert "10.0.0.1" not in json.dumps(
        [result["variables"], result["connection"], [step["text"] for step in steps]]
    )


def test_var_value_may_contain_equals_signs(device, tmp_path):
    script = write_script(tmp_path, "@timeout 5s\n@sendline set {{PAIR}}\n@expect reply:set\n")

    done = run_cli(
        script,
        "--port",
        device.path,
        "--var",
        "PAIR=a=b",
        "--result",
        tmp_path / "r.json",
        "--log-dir",
        tmp_path / "logs",
    )

    assert done.returncode == EXIT_PASSED, done.stderr
    assert device.lines == ["set a=b"]


@pytest.mark.parametrize("variable", ["NOEQUALS", "=value", "1BAD=value", "BAD-NAME=value"])
def test_malformed_variable_is_a_usage_error(device, tmp_path, variable):
    script = write_script(tmp_path, PASSING)

    done = run_cli(script, "--port", device.path, "--var", variable, "--log-dir", tmp_path / "logs")

    assert done.returncode == EXIT_ERROR
    assert "Traceback" not in done.stderr
    assert variable.split("=")[0] in done.stderr
    assert not (tmp_path / "logs").exists()


def test_failing_script_exits_one_and_records_the_failing_line(device, tmp_path):
    # The reply and the prompt arrive in one device write, so the fail pattern is checked
    # (and matches) inside the prompt wait of the @sendline on line 4, not by a timer.
    script = write_script(tmp_path, '@timeout 5s\n@prompt "> "\n@fail_on reply:ping\n@sendline ping\n')
    result_path = tmp_path / "result.json"

    done = run_cli(script, "--port", device.path, "--result", result_path, "--log-dir", tmp_path / "logs")

    assert done.returncode == EXIT_FAILED, done.stderr
    result = read_result(result_path)
    assert result["state"] == "failed"
    assert result["reason"] == "fail_pattern"
    assert result["failure"]["line"] == 4
    assert "Stopped at line 4: @sendline ping" in done.stderr
    assert "reply:ping" in done.stderr  # the output tail


def test_parse_error_exits_two_listing_every_error_and_never_opens_the_port(device, tmp_path):
    script = write_script(tmp_path, "@bogus one\n@sendline fine\n@expect {{MISSING}}\n")
    logs = tmp_path / "logs"
    result_path = tmp_path / "result.json"

    done = run_cli(script, "--port", device.path, "--result", result_path, "--log-dir", logs)

    assert done.returncode == EXIT_ERROR
    assert "Traceback" not in done.stderr
    assert "line 1" in done.stderr
    assert "line 3" in done.stderr
    assert not logs.exists()
    assert not result_path.exists()
    assert device.lines == []


def test_unreadable_script_exits_two_with_a_message(device, tmp_path):
    done = run_cli(tmp_path / "missing.serial", "--port", device.path)

    assert done.returncode == EXIT_ERROR
    assert "Traceback" not in done.stderr
    assert "missing.serial" in done.stderr


def test_missing_port_exits_two_with_a_message(tmp_path):
    script = write_script(tmp_path, PASSING)
    missing = tmp_path / "no-such-tty"

    done = run_cli(script, "--port", missing, "--var", "SERVER_IP=1", "--log-dir", tmp_path / "logs")

    assert done.returncode == EXIT_ERROR
    assert "Traceback" not in done.stderr
    assert str(missing) in done.stderr
    assert not (tmp_path / "logs").exists()


def test_port_held_exclusively_by_another_program_exits_two(device, tmp_path):
    script = write_script(tmp_path, PASSING)
    holder = os.open(device.path, os.O_RDWR | os.O_NOCTTY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the lock pyserial takes for an exclusive open
        done = run_cli(script, "--port", device.path, "--var", "SERVER_IP=1", "--log-dir", tmp_path / "logs")
    finally:
        os.close(holder)

    assert done.returncode == EXIT_ERROR
    assert "Traceback" not in done.stderr
    assert device.path in done.stderr
    assert "lock" in done.stderr
    assert device.lines == []
    assert not (tmp_path / "logs").exists()


def test_existing_result_file_exits_two_before_the_port_is_opened(device, tmp_path):
    script = write_script(tmp_path, PASSING)
    result_path = tmp_path / "result.json"
    result_path.write_text("keep me", encoding="utf-8")
    logs = tmp_path / "logs"

    done = run_cli(
        script, "--port", device.path, "--var", "SERVER_IP=1", "--result", result_path, "--log-dir", logs
    )

    assert done.returncode == EXIT_ERROR
    assert "Traceback" not in done.stderr
    assert str(result_path) in done.stderr
    assert result_path.read_text(encoding="utf-8") == "keep me"
    assert not logs.exists()
    assert device.lines == []


def test_sigint_during_a_sleep_exits_130_and_records_a_cancelled_run(device, tmp_path):
    script = write_script(tmp_path, "@sleep 60s\n")
    result_path = tmp_path / "result.json"
    process = subprocess.Popen(  # noqa: S603
        [
            *SIGINT_CLI,
            str(script),
            "--port",
            device.path,
            "--result",
            str(result_path),
            "--log-dir",
            str(tmp_path / "logs"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # A stall becomes EOF on stderr, which reaches the pytest.fail below.
    watchdog = threading.Timer(PROCESS_TIMEOUT_SECONDS, process.kill)
    watchdog.start()
    try:
        assert process.stderr is not None
        for progress in process.stderr:  # the step line proves the run is inside @sleep
            if "@sleep 60s" in progress:
                break
        else:
            pytest.fail("the CLI never reported the @sleep step")
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == EXIT_INTERRUPTED, stderr
    assert stdout == ""
    assert "Traceback" not in stderr
    assert read_result(result_path)["state"] == "cancelled"


def test_no_log_skips_the_port_log(device, tmp_path):
    script = write_script(tmp_path, PING_SCRIPT)
    logs = tmp_path / "logs"

    done = run_cli(script, "--port", device.path, "--no-log", "--log-dir", logs)

    assert done.returncode == EXIT_PASSED, done.stderr
    assert [path.suffix for path in logs.iterdir()] == [".json"]  # only the result file


def test_default_result_lands_beside_the_port_log_in_log_dir(device, tmp_path):
    script = write_script(tmp_path, PING_SCRIPT)
    logs = tmp_path / "logs"

    done = run_cli(
        script, "--port", device.path, "--log-dir", logs, "--log-format", "text", "--log-format", "jsonl"
    )

    assert done.returncode == EXIT_PASSED, done.stderr
    assert sorted(path.suffix for path in logs.iterdir()) == [".json", ".jsonl", ".txt"]


def test_a_crlf_script_is_hashed_and_numbered_from_its_raw_bytes(device, tmp_path):
    raw = b"@timeout 5s\r\n@sendline ping\r\n@expect reply:ping\r\n"
    script = tmp_path / "crlf.serial"
    script.write_bytes(raw)
    result_path = tmp_path / "result.json"

    done = run_cli(script, "--port", device.path, "--result", result_path, "--log-dir", tmp_path / "logs")

    assert done.returncode == EXIT_PASSED, done.stderr
    result = read_result(result_path)
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert [step["line"] for step in result["steps"]] == [1, 2, 3]


def test_port_log_opens_with_open_and_ends_with_close(device, tmp_path):
    script = write_script(tmp_path, PING_SCRIPT)
    logs = tmp_path / "logs"

    done = run_cli(script, "--port", device.path, "--log-dir", logs, "--log-format", "jsonl")

    assert done.returncode == EXIT_PASSED, done.stderr
    (log_path,) = logs.glob("*.jsonl")
    events = read_jsonl(log_path)
    assert events[0]["event"] == "open"
    assert events[-1]["event"] == "close"


def test_files_dir_confines_send_file_and_reaches_the_device(device, tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "payload.txt").write_bytes(b"hello\r\n")
    script = write_script(tmp_path, "@timeout 5s\n@send_file payload.txt\n@expect reply:hello\n")

    done = run_cli(script, "--port", device.path, "--files-dir", files, "--log-dir", tmp_path / "logs")

    assert done.returncode == EXIT_PASSED, done.stderr
    assert device.lines == ["hello"]


def test_send_file_cannot_leave_the_files_dir(device, tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (tmp_path / "secret.txt").write_text("secret\r\n", encoding="utf-8")
    logs = tmp_path / "logs"
    script = write_script(tmp_path, "@sendline hello\n@send_file ../secret.txt\n")

    done = run_cli(script, "--port", device.path, "--files-dir", files, "--log-dir", logs)

    assert done.returncode == EXIT_ERROR
    assert "Traceback" not in done.stderr
    assert "line 2" in done.stderr
    assert "must name a file inside the files directory" in done.stderr
    assert device.lines == []  # the step before the bad @send_file never ran
    assert not logs.exists()  # the command stopped before it opened the port


def test_baud_reaches_the_recorded_connection(device, tmp_path):
    script = write_script(tmp_path, PING_SCRIPT)
    result_path = tmp_path / "result.json"

    done = run_cli(
        script,
        "--port",
        device.path,
        "--baud",
        "9600",
        "--result",
        result_path,
        "--log-dir",
        tmp_path / "logs",
    )

    assert done.returncode == EXIT_PASSED, done.stderr
    assert read_result(result_path)["connection"]["baudrate"] == 9600


def test_unusable_log_dir_exits_two_and_leaves_the_device_usable(device, tmp_path):
    script = write_script(tmp_path, PING_SCRIPT)
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")

    done = run_cli(script, "--port", device.path, "--log-dir", blocker / "logs")

    assert done.returncode == EXIT_ERROR
    assert "Could not start the port log" in done.stderr
    assert "Traceback" not in done.stderr
    assert device.lines == []
    again = run_cli(script, "--port", device.path, "--log-dir", tmp_path / "logs")
    assert again.returncode == EXIT_PASSED, again.stderr


def test_a_stack_that_cannot_start_its_log_closes_the_port(device, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    ser = pyserial.Serial(device.path)
    try:
        with pytest.raises(script_cli._CliError, match="Could not start the port log"):
            script_cli._open_stack(ser, LogRequest(("text",), blocker / "logs"))

        assert not ser.is_open
    finally:
        ser.close()


def test_unknown_log_format_is_a_usage_error_naming_the_option(device, tmp_path):
    script = write_script(tmp_path, "@sendline hi\n")

    done = run_cli(script, "--port", device.path, "--log-format", "bogus", "--log-dir", tmp_path / "logs")

    assert done.returncode == EXIT_ERROR
    assert "Traceback" not in done.stderr
    assert "'bogus' in --log-format" in done.stderr
    assert "log_formats" not in done.stderr  # the MCP argument name
    assert device.lines == []


def test_variable_name_that_cannot_start_with_a_digit_is_a_usage_error(device, tmp_path):
    script = write_script(tmp_path, "@sendline hi\n")

    done = run_cli(script, "--port", device.path, "--var", "1BAD=x", "--log-dir", tmp_path / "logs")

    assert done.returncode == EXIT_ERROR
    assert "Invalid variable name '1BAD'" in done.stderr
    assert device.lines == []


def test_invalid_max_seconds_exits_two_with_a_message_before_opening_the_port(device, tmp_path, monkeypatch):
    script = write_script(tmp_path, PING_SCRIPT)
    monkeypatch.setenv("SERIAL_MCP_SCRIPT_MAX_SECONDS", "abc")
    logs = tmp_path / "logs"

    done = run_cli(script, "--port", device.path, "--log-dir", logs, "--log-format", "jsonl")

    assert done.returncode == EXIT_ERROR
    assert "SERIAL_MCP_SCRIPT_MAX_SECONDS" in done.stderr
    assert "Traceback" not in done.stderr
    assert device.lines == []
    assert not logs.exists()


def test_result_that_cannot_be_written_exits_two_and_is_not_reported(device, tmp_path):
    script = write_script(tmp_path, PING_SCRIPT)
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    result_path = blocker / "result.json"

    done = run_cli(script, "--port", device.path, "--result", result_path, "--log-dir", tmp_path / "logs")

    assert done.returncode == EXIT_ERROR
    assert "file_error" in done.stderr
    assert "Result:" not in done.stderr
    assert not result_path.exists()


def test_an_unexpected_exception_exits_two_with_its_traceback(tmp_path, monkeypatch, capsys):
    script = write_script(tmp_path, PASSING)

    def explode(*_args, **_kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(script_cli, "parse", explode)

    status = script_cli.main([str(script), "--port", "/dev/null"])

    assert status == EXIT_ERROR
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "parser exploded" in err


def test_a_second_interrupt_does_not_skip_releasing_the_connection(monkeypatch):
    calls = []

    def interrupted_once(conns):
        calls.append(conns)
        if len(calls) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(script_cli, "stop_connections", interrupted_once)
    conn = object()

    script_cli._release(conn)

    assert calls == [[conn], [conn]]


def test_a_second_interrupt_does_not_skip_waiting_for_the_cancelled_run():
    class Runner:
        def __init__(self) -> None:
            self.cancelled = False
            self.joins = 0

        def cancel(self) -> None:
            self.cancelled = True

        def join(self, _timeout: float) -> None:
            self.joins += 1
            if self.joins == 1:
                raise KeyboardInterrupt

    runner = Runner()

    script_cli._cancel_run(runner)

    assert runner.cancelled
    assert runner.joins == 2


@pytest.mark.parametrize("module", ["serial_mcp_server.script_cli", "serial_mcp_server.port_stack"])
def test_importing_the_cli_path_leaves_mcp_out(module):
    probe = f"import sys, {module}; sys.exit(1 if any(n == 'mcp' or n.startswith('mcp.') for n in sys.modules) else 0)"

    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT_SECONDS,
        check=False,
    )

    assert done.returncode == 0, done.stderr
