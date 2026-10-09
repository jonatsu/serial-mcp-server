"""The ``serial_mcp_script`` command: run one serial script on a port, with no agent and no MCP server.

The outcome is the exit status plus the result file: 0 passed, 1 failed, 2 errored or unusable
input, 130 interrupted. Everything here is synchronous, and nothing imports ``mcp``.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import time
import traceback
from collections.abc import Sequence
from pathlib import Path

import serial as pyserial

from serial_mcp_server.connection_session import ConnectionSession
from serial_mcp_server.paths import files_dir
from serial_mcp_server.port_stack import PortLogStartError, conn_config, open_port_stack
from serial_mcp_server.portlog import LogRequest, build_log_request, log_directory
from serial_mcp_server.script import Script, ScriptSyntaxError, parse
from serial_mcp_server.script.runner import RunMetadata, RunResult, RunState, ScriptRunner
from serial_mcp_server.state import SCRIPT_CANCEL_WAIT_SECONDS, SerialConnection, stop_connections
from serial_mcp_server.writer import NO_PACING

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130
_POLL_SECONDS = 0.2
_DEFAULT_BAUD = 115200
_TIMEOUT_SECONDS = 0.2
_ENCODING = "utf-8"
_NEWLINE = "\r\n"
_CONNECTION_ID = "cli"
_EXIT_BY_STATE = {RunState.PASSED: EXIT_PASSED, RunState.FAILED: EXIT_FAILED}


class _CliError(Exception):
    """A failure already worded for the user; ``main`` prints it and exits with ``EXIT_ERROR``."""


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _variable(text: str) -> tuple[str, str]:
    name, separator, value = text.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"{text!r} is not NAME=VALUE.")
    return name, value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serial_mcp_script",
        description="Run one serial script on a port. Exit status: 0 passed, 1 failed, "
        "2 errored or unusable input, 130 interrupted.",
    )
    parser.add_argument("script", type=Path, help="Path of the script to run.")
    parser.add_argument("--port", required=True, help="Serial port to open, such as /dev/ttyUSB0.")
    parser.add_argument("--baud", type=int, default=_DEFAULT_BAUD, help="Baud rate (default: %(default)s).")
    parser.add_argument(
        "--var",
        type=_variable,
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Set a script variable; repeat for several.",
    )
    parser.add_argument(
        "--log-format",
        action="append",
        metavar="FORMAT",
        help="Port log format: text, jsonl, lines, or raw. Repeat for several. "
        "Default: SERIAL_MCP_LOG_FORMATS, else text.",
    )
    parser.add_argument("--no-log", action="store_true", help="Do not record a port log.")
    parser.add_argument(
        "--log-dir",
        type=Path,
        help="Directory for the port log and, unless --result is set, the result file. "
        "Default: SERIAL_MCP_LOG_DIR, else .serial_mcp/logs/.",
    )
    parser.add_argument(
        "--files-dir",
        type=Path,
        help="Directory @send_file reads from. Default: SERIAL_MCP_FILES_DIR, else .serial_mcp/files/.",
    )
    parser.add_argument("--result", type=Path, help="Write the result file here. The path must not exist.")
    return parser


def _load_script(path: Path, variables: dict[str, str], files_root: Path) -> Script:
    """Read and parse the script, as the MCP tool does: from raw bytes, so hash and line numbers agree."""
    try:
        source = path.read_bytes().decode(_ENCODING)
    except (OSError, UnicodeError) as exc:
        raise _CliError(f"cannot read script {path}: {exc}") from exc
    try:
        return parse(source, variables, files_root)
    except ScriptSyntaxError as exc:
        lines = [f"{path} has {len(exc.errors)} error(s); nothing was sent."]
        lines += [f"  line {error.line}: {error.message}\n    {error.text}" for error in exc.errors]
        raise _CliError("\n".join(lines)) from exc
    except ValueError as exc:  # an invalid variable name; parse() words the rule
        raise _CliError(str(exc)) from exc
    except OSError as exc:
        raise _CliError(f"cannot check the script's files: {exc}") from exc


def _open_serial(port: str, baud: int) -> pyserial.Serial:
    try:
        return pyserial.Serial(
            port=port,
            baudrate=baud,
            bytesize=pyserial.EIGHTBITS,
            parity=pyserial.PARITY_NONE,
            stopbits=pyserial.STOPBITS_ONE,
            timeout=_TIMEOUT_SECONDS,
            write_timeout=_TIMEOUT_SECONDS,
            # Refuse a port another program holds exclusively; a plain open would share it silently.
            exclusive=True,
        )
    except (OSError, ValueError) as exc:  # pyserial's SerialException is an OSError
        raise _CliError(f"cannot open port {port}: {exc}") from exc


def _report_progress(runner: ScriptRunner) -> None:
    """Print one stderr line each time the current step changes, until the run is done."""
    last_line: int | None = None
    while not runner.done.wait(_POLL_SECONDS):
        status = runner.snapshot()
        if status.current_line is not None and status.current_line != last_line:
            last_line = status.current_line
            _say(f"line {status.current_line}: {status.current_text}")


def _cancel_run(runner: ScriptRunner) -> None:
    """Cancel the run and wait for it, bounded; a further Ctrl-C does not shorten the teardown."""
    runner.cancel()
    deadline = time.monotonic() + SCRIPT_CANCEL_WAIT_SECONDS
    while True:
        try:
            runner.join(max(0.0, deadline - time.monotonic()))
            return
        except KeyboardInterrupt:
            continue


def _release(conn: SerialConnection) -> None:
    """Stop the writer and reader, close the log and the port; Ctrl-C cannot skip any of it."""
    while True:
        try:
            stop_connections([conn])
            return
        except KeyboardInterrupt:
            continue


def _summarize(runner: ScriptRunner, result: RunResult | None) -> None:
    if result is None:
        _say("No result: the run stopped before it finished.")
        return
    reason = f" ({result.reason})" if result.reason else ""
    _say(f"Run {result.state.value}{reason}.")
    failure = result.failure
    if failure is not None:
        if failure.line is not None:
            _say(f"Stopped at line {failure.line}: {failure.text}")
        if failure.detail:
            _say(failure.detail)
        if failure.output_tail:
            _say(f"Device output before it stopped:\n{failure.output_tail}")
    result_path = runner.result_path
    if result_path is not None and result_path.is_file():  # a file_error run never wrote it
        _say(f"Result: {result_path}")
    for path in result.log_paths:
        _say(f"Log: {path}")


def _run(runner: ScriptRunner) -> int:
    interrupted = False
    try:
        runner.start()
        _report_progress(runner)
    except KeyboardInterrupt:
        interrupted = True
        _say("Interrupted: cancelling the run.")
        _cancel_run(runner)
    result = runner.result
    _summarize(runner, result)
    if interrupted:
        return EXIT_INTERRUPTED
    if result is None:
        return EXIT_ERROR
    return _EXIT_BY_STATE.get(result.state, EXIT_ERROR)


def _log_request(formats: list[str] | None, directory: Path) -> LogRequest:
    try:
        request = build_log_request(formats, formats_origin="--log-format")
    except ValueError as exc:
        raise _CliError(str(exc)) from exc
    # The port log and the default result file share one directory, so --log-dir moves both.
    return dataclasses.replace(request, directory=directory)


def _open_stack(ser: pyserial.Serial, log_request: LogRequest | None) -> SerialConnection:
    """Build the port stack on *ser*, which this closes if the stack cannot be built.

    The connection records what *ser* reports, so it cannot drift from the port that was opened.
    """
    try:
        return open_port_stack(
            ser,
            port=ser.port,
            baudrate=ser.baudrate,
            bytesize=ser.bytesize,
            parity=ser.parity,
            stopbits=ser.stopbits,
            timeout=ser.timeout,
            write_timeout=ser.write_timeout,
            encoding=_ENCODING,
            newline=_NEWLINE,
            pacing=NO_PACING,
            log_request=log_request,
            connection_id=_CONNECTION_ID,
            mirror_mode="off",
            mirror_link=None,
        )
    except PortLogStartError as exc:
        ser.close()
        raise _CliError(str(exc)) from exc
    except BaseException:
        ser.close()
        raise


def _make_runner(
    script: Script,
    conn: SerialConnection,
    files_root: Path,
    *,
    script_name: str,
    result_directory: Path,
    result_path: Path | None,
) -> ScriptRunner:
    try:
        return ScriptRunner(
            script,
            ConnectionSession(conn, files_root),
            metadata=RunMetadata(
                script_name=script_name,
                connection_config=conn_config(conn),
                result_directory=result_directory,
                log_paths=conn.log_slot.file_paths,
                result_path=result_path,
            ),
        )
    except ValueError as exc:  # for example an unusable SERIAL_MCP_SCRIPT_MAX_SECONDS
        raise _CliError(str(exc)) from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        files_root = args.files_dir or files_dir()
        log_dir = args.log_dir or log_directory()
        script = _load_script(args.script, dict(args.var), files_root)
        # lexists, so a dangling symlink counts as existing: the runner would refuse to create through it.
        if args.result is not None and os.path.lexists(args.result):
            raise _CliError(f"result file {args.result} already exists; choose another path or remove it.")
        log_request = None if args.no_log else _log_request(args.log_format, log_dir)

        conn = _open_stack(_open_serial(args.port, args.baud), log_request)
        try:
            runner = _make_runner(
                script,
                conn,
                files_root,
                script_name=args.script.name,
                result_directory=log_dir,
                result_path=args.result,
            )
            return _run(runner)
        finally:
            _release(conn)
    except _CliError as exc:
        _say(f"serial_mcp_script: {exc}")
        return EXIT_ERROR
    except Exception:  # exit 1 means only "the script failed", so an unexpected error must not leak it
        traceback.print_exc()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
