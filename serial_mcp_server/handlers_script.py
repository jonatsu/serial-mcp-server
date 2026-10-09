"""MCP tools for running, inspecting, and discovering serial scripts."""

from __future__ import annotations

import asyncio
import logging
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.confined_files import read_confined_file
from serial_mcp_server.helpers import _coerce_bool, _err, _ok
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.stack import conn_config
from serial_mcp_server.portlog.formats import CLOSE_EVENT, LOG_FORMATS_SCHEMA, OPEN_EVENT
from serial_mcp_server.portlog.log import PortLog
from serial_mcp_server.portlog.request import build_log_request
from serial_mcp_server.script import DIRECTIVES, ScriptSyntaxError, parse
from serial_mcp_server.script.connection_session import ConnectionSession
from serial_mcp_server.script.results import RunMetadata
from serial_mcp_server.script.runner import SCRIPT_CANCEL_WAIT_SECONDS, ScriptRunner
from serial_mcp_server.state import ScriptRun, SerialState

logger = logging.getLogger("serial_mcp_server")

MAX_SCRIPT_BYTES = 256 * 1024
MAX_WAIT_MS = 60_000

TOOLS: list[Tool] = [
    Tool(
        name="serial.script.run",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Parse and run a serial script on one open connection. Pass exactly one of a confined script path "
            "or inline source; the script owns that connection's writes and control lines until it finishes. "
            "The default starts a port log when none is active. See docs/scripts.md for the language reference."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "path": {"type": "string", "description": "Script path relative to SERIAL_MCP_SCRIPTS_DIR."},
                "source": {"type": "string", "description": "Inline UTF-8 script source, up to 256 KiB."},
                "vars": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Variables substituted by the script parser.",
                },
                "log": {"type": ["boolean", "string"], "default": True},
                "log_formats": LOG_FORMATS_SCHEMA,
                "wait_ms": {
                    "type": ["integer", "string"],
                    "default": 0,
                    "description": "Wait at most 0–60000 milliseconds for a result; default 0 returns immediately.",
                },
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="serial.script.status",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description="Inspect a script run by run_id, or the latest run on a connection_id.",
        inputSchema={
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "connection_id": {"type": "string"}},
            "required": [],
        },
    ),
    Tool(
        name="serial.script.cancel",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
        description="Request cancellation of a script run and wait up to five seconds for it to stop.",
        inputSchema={"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    ),
    Tool(
        name="serial.script.list",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description="List regular script files in SERIAL_MCP_SCRIPTS_DIR with sizes and first comment lines.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="serial.script.template",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description="Return the script directive reference and an example script skeleton.",
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
        _resolved, payload = await asyncio.to_thread(
            read_confined_file, scripts_root, requested, MAX_SCRIPT_BYTES
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
        script = await asyncio.to_thread(parse, source, variables, files_root)
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
                created_log = await asyncio.to_thread(PortLog.start, log_request, conn.encoding)
            except OSError as exc:
                state.script_jobs.release_unstarted(run.run_id)
                return _err("log_failed", f"Could not start the port log in {log_request.directory}: {exc}")
            if state.connections.get(connection_id) is not conn:
                await asyncio.to_thread(created_log.discard)
                state.script_jobs.release_unstarted(run.run_id)
                return _err("not_found", f"Connection {connection_id} closed while the script was starting.")
            if conn.log_slot.log is None:
                created_log.event(OPEN_EVENT, config=conn_config(conn))
                conn.log_slot.set(created_log)
                started_log = created_log
            else:
                await asyncio.to_thread(created_log.discard)

        if state.connections.get(connection_id) is not conn:
            state.script_jobs.release_unstarted(run.run_id)
            return _err("not_found", f"Connection {connection_id} closed while the script was starting.")
        runner.start()
        runner_started = True
        state.script_jobs.watch(run, cleanup_log)
    except BaseException:
        if runner_started:
            runner.cancel()
            await asyncio.to_thread(runner.join)
            await asyncio.to_thread(_close_owned_log, conn, started_log)
            state.script_jobs.finish(run.run_id)
        else:
            state.script_jobs.release_unstarted(run.run_id)
            await asyncio.to_thread(_close_owned_log, conn, started_log)
        raise

    if wait_ms:
        wait_seconds = wait_ms / 1000.0
        deadline = time.monotonic() + wait_seconds
        ended = await asyncio.to_thread(runner.done.wait, wait_seconds)
        if ended:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining and not run.finished.is_set():
                await asyncio.to_thread(run.finished.wait, remaining)
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
        await asyncio.to_thread(run.finished.wait, 5.0)
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
    await asyncio.to_thread(run.runner.join, SCRIPT_CANCEL_WAIT_SECONDS)
    if run.runner.done.is_set():
        remaining = max(0.0, deadline - time.monotonic())
        if remaining and not run.finished.is_set():
            await asyncio.to_thread(run.finished.wait, remaining)
        status = _job_status(run, include_runner_result=True)
        return _ok(run_id=run.run_id, state=status["state"], result=status.get("result"))
    return _ok(run_id=run.run_id, state=run.runner.snapshot().state.value, cancel_requested=True)


def _list_scripts(root: Path) -> list[dict[str, Any]]:
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
        scripts = await asyncio.to_thread(_list_scripts, state.settings.scripts_dir)
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
