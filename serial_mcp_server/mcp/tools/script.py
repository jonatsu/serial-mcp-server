"""MCP tools for running, inspecting, and discovering serial scripts."""

from __future__ import annotations

import logging
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel
import anyio.to_thread
from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.confined_files import read_confined_file
from serial_mcp_server.mcp.responses import _coerce_bool, _err, _ok
from serial_mcp_server.mcp.state import ScriptRun, SerialState
from serial_mcp_server.mcp.threads import run_to_completion
from serial_mcp_server.mcp.tools.schemas import CONNECTION_ID
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.stack import conn_config
from serial_mcp_server.portlog.formats import CLOSE_EVENT, LOG_FORMATS_SCHEMA, OPEN_EVENT
from serial_mcp_server.portlog.log import PortLog
from serial_mcp_server.portlog.request import build_log_request
from serial_mcp_server.script import DIRECTIVES, ScriptSyntaxError, parse
from serial_mcp_server.script.connection_session import ConnectionSession
from serial_mcp_server.script.results import RunMetadata
from serial_mcp_server.script.runner import SCRIPT_CANCEL_WAIT_SECONDS, ScriptRunner

logger = logging.getLogger("serial_mcp_server")

# Largest script, in UTF-8 bytes, whether inline or from a file. A larger one is rejected, not cut.
MAX_SCRIPT_BYTES = 256 * 1024
# Largest ``wait_ms`` of ``serial.script.run``. A larger value is rejected, not clamped.
MAX_WAIT_MS = 60_000

_RUN_ID = {"type": "string", "description": "The run_id that serial.script.run returned."}

TOOLS: list[Tool] = [
    Tool(
        name="serial.script.run",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use for a repeatable sequence, such as a smoke test, a boot check, or a configuration that sends "
            "commands and checks each reply, instead of many serial.write and serial.read calls. Runs a serial "
            "script on one open connection in the background and returns a run_id; serial.script.template "
            "returns the language reference. Pass exactly one of path or source. Nothing is sent unless the "
            "whole script parses. The run owns the connection's writes and control lines until it ends; reads "
            "still work. Poll serial.script.status for the outcome, or pass wait_ms to wait for it here."
            " Run deadlines and failure patterns remain active while writes wait; a stop cancels queued writes "
            "or interrupts pacing, then waits for any active serial call to settle before the run ends."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "path": {
                    "type": "string",
                    "description": "Script file relative to the scripts directory (SERIAL_MCP_SCRIPTS_DIR); serial.script.list shows them.",
                },
                "source": {"type": "string", "description": "Inline UTF-8 script source, up to 256 KiB."},
                "vars": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Values for the script's {{NAME}} variables. Results and status list only the names, but a value still reaches the device, the port log, and any @mark label or device echo that contains it.",
                },
                "log": {
                    "type": ["boolean", "string"],
                    "default": True,
                    "description": "Record the run in a port log, unless one is already active (default true).",
                },
                "log_formats": LOG_FORMATS_SCHEMA,
                "wait_ms": {
                    "type": ["integer", "string"],
                    "default": 0,
                    "description": "Wait at most 0 to 60000 milliseconds for the run to end; default 0 returns at once.",
                },
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="serial.script.status",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use after serial.script.run to follow a run and get its outcome: state, the current line, completed "
            "steps, and elapsed time; once the run ends, the result, its failure with the device output that "
            "preceded it, and the result and log file paths. Pass exactly one of run_id or connection_id, which "
            "selects the latest run on that connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {"run_id": _RUN_ID, "connection_id": CONNECTION_ID},
            "required": [],
        },
    ),
    Tool(
        name="serial.script.cancel",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
        description=(
            "Use when the user wants a running script stopped, or a run is stuck: requests cancellation and "
            "waits up to five seconds for it to stop. The device stays where the script left it."
        ),
        inputSchema={"type": "object", "properties": {"run_id": _RUN_ID}, "required": ["run_id"]},
    ),
    Tool(
        name="serial.script.list",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use before writing a new script, to find one the user already keeps: lists the script files in the "
            "scripts directory (SERIAL_MCP_SCRIPTS_DIR) with their sizes and first comment lines."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="serial.script.template",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use before writing a script: returns the language reference (lines, directives, variables, "
            "matching) and an example script to adapt."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


def _parse_wait_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("wait_ms must be an integer from 0 to 60000.")
    try:
        wait_ms = int(value)
    except ValueError as exc:
        raise ValueError("wait_ms must be an integer from 0 to 60000.") from exc
    if not 0 <= wait_ms <= MAX_WAIT_MS:
        raise ValueError("wait_ms must be an integer from 0 to 60000.")
    return wait_ms


def _parse_variables(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("vars must be a map of string names to string values.")
    parsed: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, str):
            raise ValueError("vars must be a map of string names to string values.")
        parsed[name] = item
    return parsed


async def _script_source(args: dict[str, Any], scripts_root: Path) -> tuple[str, str] | dict[str, Any]:
    """Validate and load one inline or confined script source."""
    has_path = "path" in args
    has_source = "source" in args
    if has_path == has_source:
        return _err("invalid_params", "Pass exactly one of path or source.")
    if has_source:
        source = args["source"]
        if not isinstance(source, str):
            return _err("invalid_params", "source must be a string.")
        try:
            size = len(source.encode("utf-8"))
        except UnicodeError as exc:
            return _err("invalid_params", str(exc))
        if size > MAX_SCRIPT_BYTES:
            return _err("invalid_params", f"source exceeds the {MAX_SCRIPT_BYTES}-byte limit.")
        return source, "inline"

    requested = args["path"]
    if not isinstance(requested, str):
        return _err("invalid_params", "path must be a relative string.")
    try:
        _resolved, payload = await anyio.to_thread.run_sync(
            read_confined_file, scripts_root, requested, MAX_SCRIPT_BYTES, abandon_on_cancel=True
        )
        return payload.decode("utf-8"), requested
    except UnicodeError as exc:
        return _err("invalid_params", f"Script {requested!r} is not valid UTF-8: {exc}")
    except ValueError as exc:
        return _err("invalid_params", str(exc))
    except OSError as exc:
        return _err("file_unreadable", f"Could not read script {requested!r}: {exc.strerror or exc}")


def _parse_error_result(error: ScriptSyntaxError) -> dict[str, Any]:
    result = _err("invalid_params", str(error))
    result["errors"] = [
        {"line": item.line, "text": item.text, "message": item.message} for item in error.errors
    ]
    return result


def _close_owned_log(conn: SerialConnection, owned_log: PortLog | None) -> None:
    if owned_log is None or conn.log_slot.log is not owned_log:
        return
    log = conn.log_slot.detach()
    if log is None or log is not owned_log:
        return
    log.event(CLOSE_EVENT)
    log.close()


def _job_status(run: ScriptRun, *, include_runner_result: bool = False) -> dict[str, Any]:
    snapshot = run.runner.snapshot()
    end = run.finished_at if run.finished_at is not None else time.monotonic()
    result: dict[str, Any] = {
        "run_id": run.run_id,
        "connection_id": run.connection_id,
        "state": snapshot.state.value,
        "current_line": snapshot.current_line,
        "current_text": snapshot.current_text,
        "completed_steps": snapshot.completed_steps,
        "elapsed_seconds": max(0.0, end - run.started_at),
        "variables": list(snapshot.variables),
    }
    finished = run.result
    result_path = run.result_path
    if finished is None and include_runner_result and run.runner.done.is_set():
        finished = run.runner.result
        result_path = run.runner.result_path
    if finished is not None:
        failure = finished.failure.to_json() if finished.failure is not None else None
        result.update(
            result=finished.to_json(),
            result_path=str(result_path) if result_path is not None else None,
            log_paths=list(finished.log_paths),
            failure=failure,
            output_tail=finished.failure.output_tail if finished.failure is not None else "",
        )
    return _ok(**result)


async def handle_script_run(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    """Parse a script, start it in the background, and optionally wait up to ``wait_ms`` for its end.

    With ``log`` true (the default) the run starts a port log unless the connection already has one, and
    closes that log when the run ends; a log that was already active is left running. Only the last 20
    finished runs stay queryable (``ScriptJobs``).
    """
    connection_id = args["connection_id"]
    if not isinstance(connection_id, str):
        return _err("invalid_params", "connection_id must be a string.")
    conn = state.get_connection(connection_id)

    try:
        variables = _parse_variables(args.get("vars"))
    except ValueError as exc:
        return _err("invalid_params", str(exc))

    files_root = state.settings.files_dir
    loaded = await _script_source(args, state.settings.scripts_dir)
    if isinstance(loaded, dict):
        return loaded
    source, script_name = loaded
    try:
        script = await anyio.to_thread.run_sync(parse, source, variables, files_root, abandon_on_cancel=True)
    except ScriptSyntaxError as exc:
        return _parse_error_result(exc)
    except ValueError as exc:
        return _err("invalid_params", str(exc))
    except OSError as exc:
        return _err("file_unreadable", str(exc))

    try:
        wait_ms = _parse_wait_ms(args.get("wait_ms", 0))
    except ValueError as exc:
        return _err("invalid_params", str(exc))

    log_value = args.get("log", True)
    if not isinstance(log_value, (bool, str)):
        return _err("invalid_params", "log must be a boolean.")
    should_log = _coerce_bool(log_value)
    try:
        log_request = (
            build_log_request(state.settings, args.get("log_formats"))
            if should_log and conn.log_slot.log is None
            else None
        )
    except ValueError as exc:
        return _err("invalid_params", str(exc))

    runner = ScriptRunner(
        script,
        ConnectionSession(conn, files_root),
        metadata=RunMetadata(
            script_name=script_name,
            connection_config=conn_config(conn),
            result_directory=state.settings.log_dir,
            log_paths=conn.log_slot.file_paths,
        ),
        pattern_timeout_seconds=state.settings.script_pattern_seconds,
        max_seconds=state.settings.script_max_seconds,
    )
    run, existing_run_id = state.script_jobs.claim(connection_id, runner)
    if run is None:
        if existing_run_id is None:
            return _err("script_running", "Another script run is active on this connection.")
        return _err("script_running", f"Script run {existing_run_id} is running on this connection.")

    started_log: PortLog | None = None
    runner_started = False

    def cleanup_log() -> None:
        _close_owned_log(conn, started_log)

    try:
        if log_request is not None and conn.log_slot.log is None:
            try:
                created_log = await anyio.to_thread.run_sync(PortLog.start, log_request, conn.encoding)
            except OSError as exc:
                state.script_jobs.release_unstarted(run.run_id)
                return _err("log_failed", f"Could not start the port log in {log_request.directory}: {exc}")
            if state.connections.get(connection_id) is not conn:
                await run_to_completion(created_log.discard)
                state.script_jobs.release_unstarted(run.run_id)
                return _err("not_found", f"Connection {connection_id} closed while the script was starting.")
            if conn.log_slot.log is None:
                created_log.event(OPEN_EVENT, config=conn_config(conn))
                conn.log_slot.set(created_log)
                started_log = created_log
            else:
                await run_to_completion(created_log.discard)

        # PortLog.start is waited for even when the request is cancelled; the cancellation ends the request
        # here, before the run starts, and the handler below closes the log this request started.
        await anyio.lowlevel.checkpoint()
        if state.connections.get(connection_id) is not conn:
            state.script_jobs.release_unstarted(run.run_id)
            return _err("not_found", f"Connection {connection_id} closed while the script was starting.")
        runner.start()
        runner_started = True
        state.script_jobs.watch(run, cleanup_log)
    except BaseException:
        if runner_started:
            runner.cancel()
            await run_to_completion(runner.join)
            await run_to_completion(_close_owned_log, conn, started_log)
            state.script_jobs.finish(run.run_id)
        else:
            state.script_jobs.release_unstarted(run.run_id)
            await run_to_completion(_close_owned_log, conn, started_log)
        raise

    if wait_ms:
        wait_seconds = wait_ms / 1000.0
        deadline = time.monotonic() + wait_seconds
        ended = await anyio.to_thread.run_sync(runner.done.wait, wait_seconds, abandon_on_cancel=True)
        if ended:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining and not run.finished.is_set():
                await anyio.to_thread.run_sync(run.finished.wait, remaining, abandon_on_cancel=True)
            return _job_status(run, include_runner_result=True)
    return _ok(run_id=run.run_id, state=runner.snapshot().state.value)


async def handle_script_status(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    run_id = args.get("run_id")
    connection_id = args.get("connection_id")
    if (run_id is None) == (connection_id is None):
        return _err("invalid_params", "Pass exactly one of run_id or connection_id.")
    if run_id is None:
        if not isinstance(connection_id, str):
            return _err("invalid_params", "connection_id must be a string.")
        run = state.script_jobs.latest_for_connection(connection_id)
    else:
        if not isinstance(run_id, str):
            return _err("invalid_params", "run_id must be a string.")
        run = state.script_jobs.find(run_id)
    if run is None:
        identifier = f"run_id {run_id!r}" if run_id is not None else f"connection_id {connection_id!r}"
        return _err("not_found", f"No retained script run for {identifier}.")
    if run.runner.done.is_set() and not run.finished.is_set():
        # The runner has a result, but its watcher thread may still be closing the run's log. Wait up to 5 s
        # for it, so the status carries the final result and log paths; after that, report what there is.
        await anyio.to_thread.run_sync(run.finished.wait, 5.0, abandon_on_cancel=True)
    return _job_status(run)


async def handle_script_cancel(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    run_id = args["run_id"]
    if not isinstance(run_id, str):
        return _err("invalid_params", "run_id must be a string.")
    run = state.script_jobs.find(run_id)
    if run is None:
        return _err("not_found", f"No script run with run_id {run_id!r}.")
    if run.finished.is_set():
        return _job_status(run)

    deadline = time.monotonic() + SCRIPT_CANCEL_WAIT_SECONDS
    run.runner.cancel()
    await anyio.to_thread.run_sync(run.runner.join, SCRIPT_CANCEL_WAIT_SECONDS, abandon_on_cancel=True)
    if run.runner.done.is_set():
        remaining = max(0.0, deadline - time.monotonic())
        if remaining and not run.finished.is_set():
            await anyio.to_thread.run_sync(run.finished.wait, remaining, abandon_on_cancel=True)
        status = _job_status(run, include_runner_result=True)
        return _ok(run_id=run.run_id, state=status["state"], result=status.get("result"))
    return _ok(run_id=run.run_id, state=run.runner.snapshot().state.value, cancel_requested=True)


def _list_scripts(root: Path) -> list[dict[str, Any]]:
    """List the regular files directly in *root*, by name, with their sizes and first comment lines.

    Subdirectories and symbolic links are skipped, and a missing directory gives an empty list. A file that
    cannot be read is logged and left out.
    """
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return []
    scripts: list[dict[str, Any]] = []
    for path in entries:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            description = None
            with path.open("r", encoding="utf-8", errors="replace") as source_file:
                for line in source_file:
                    if line.lstrip().startswith("#"):
                        description = line.lstrip()[1:].strip() or None
                        break
            scripts.append({"name": path.name, "size": info.st_size, "description": description})
        except OSError as exc:
            logger.warning("Skipping unreadable script %s: %s", path.name, exc)
            continue
    return scripts


async def handle_script_list(state: SerialState, _args: dict[str, Any]) -> dict[str, Any]:
    try:
        scripts = await anyio.to_thread.run_sync(
            _list_scripts, state.settings.scripts_dir, abandon_on_cancel=True
        )
    except OSError as exc:
        return _err("file_unreadable", f"Could not list scripts: {exc.strerror or exc}")
    return _ok(scripts=scripts, count=len(scripts))


def _table_cell(text: str) -> str:
    """Escape pipes, which would otherwise split a Markdown table cell (a code span does not protect them)."""
    return text.replace("|", "\\|")


def _script_template() -> str:
    directives = [
        f"| `{directive.name}` | `{_table_cell(directive.usage)}` | {_table_cell(directive.summary)} |"
        for directive in DIRECTIVES.values()
    ]
    reference = "\n".join(directives)
    return f"""# Serial script template

## Directives

| Name | Syntax | Effect |
| --- | --- | --- |
{reference}

## Echo pitfall

Devices often echo commands. An `@expect` immediately after a command may match its echo instead of the device's
response. Anchor expects on text the echo cannot contain, or configure `@prompt` to consume output through the prompt.

## Skeleton

```text
# Describe the device and test
@timeout 10s
@prompt --off
@fail_on "(?i)error|panic"

version
@expect "device version"
@mark version-checked
```
"""


async def handle_script_template(_state: SerialState, _args: dict[str, Any]) -> dict[str, Any]:
    return _ok(template=_script_template())


HANDLERS: dict[str, Any] = {
    "serial.script.run": handle_script_run,
    "serial.script.status": handle_script_status,
    "serial.script.cancel": handle_script_cancel,
    "serial.script.list": handle_script_list,
    "serial.script.template": handle_script_template,
}
