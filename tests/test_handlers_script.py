"""Behavioral tests for serial.script.* tools and their connection ownership."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serial_mcp_server.handlers_file import handle_send_file
from serial_mcp_server.handlers_log import (
    handle_log_mark,
    handle_log_start,
    handle_log_status,
    handle_log_stop,
)
from serial_mcp_server.handlers_script import (
    HANDLERS,
    TOOLS,
    handle_script_cancel,
    handle_script_list,
    handle_script_run,
    handle_script_status,
    handle_script_template,
)
from serial_mcp_server.handlers_serial import (
    handle_close,
    handle_configure,
    handle_connection_status,
    handle_flush,
    handle_pulse_dtr,
    handle_pulse_rts,
    handle_read,
    handle_set_dtr,
    handle_set_rts,
    handle_write,
)
from serial_mcp_server.mirror import SerialBuffer
from serial_mcp_server.portlog import ENV_DIR, ENV_FORMATS, ENV_LOG, ENV_TEXT_TIMESTAMPS
from serial_mcp_server.script.directives import DIRECTIVES
from serial_mcp_server.script.runner import ScriptRunner
from serial_mcp_server.writer import SerialWriter

SID = {"connection_id": "s1"}


@pytest.fixture
def script_env(monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    files = tmp_path / "files"
    files.mkdir()
    monkeypatch.setenv("SERIAL_MCP_SCRIPTS_DIR", str(scripts))
    monkeypatch.setenv("SERIAL_MCP_FILES_DIR", str(files))
    monkeypatch.setenv(ENV_DIR, str(tmp_path / "logs"))
    monkeypatch.delenv(ENV_LOG, raising=False)
    monkeypatch.delenv(ENV_FORMATS, raising=False)
    monkeypatch.delenv(ENV_TEXT_TIMESTAMPS, raising=False)
    return scripts


@pytest.fixture
def another_connection(connected_entry):
    state, original = connected_entry
    serial = MagicMock()
    serial.is_open = True
    serial.write.side_effect = len
    writer = SerialWriter(serial)
    writer.start()
    conn = replace(
        original,
        connection_id="s2",
        port="/dev/ttyUSB1",
        ser=serial,
        writer=writer,
        buffer=SerialBuffer(),
    )
    state.connections[conn.connection_id] = conn
    yield conn
    writer.stop()
    writer.join()


async def _running_script(state, source: str = "@sleep 60s\n") -> str:
    result = await handle_script_run(state, {**SID, "source": source, "log": False})
    assert result["ok"]
    return result["run_id"]


async def _wait_for_status(state, run_id: str, condition, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await handle_script_status(state, {"run_id": run_id})
        if condition(result):
            return result
        await asyncio.sleep(0.01)
    raise AssertionError(f"script status did not reach the expected state: {result}")


class TestRegistration:
    def test_five_tools_are_registered_with_annotations(self):
        expected = {
            "serial.script.run",
            "serial.script.status",
            "serial.script.cancel",
            "serial.script.list",
            "serial.script.template",
        }

        assert {tool.name for tool in TOOLS} == set(HANDLERS) == expected
        assert all(tool.annotations is not None for tool in TOOLS)


class TestRun:
    async def test_parse_errors_include_each_line_and_send_nothing(self, connected_entry, script_env):
        state, conn = connected_entry

        result = await handle_script_run(
            state,
            {**SID, "source": "@unknown thing\n@sendhex xyz\n", "log": False},
        )

        assert result["error"]["code"] == "invalid_params"
        assert [(error["line"], error["text"]) for error in result["errors"]] == [
            (1, "@unknown thing"),
            (2, "@sendhex xyz"),
        ]
        conn.ser.write.assert_not_called()
        assert state.script_jobs.active_for_connection("s1") is None

    @pytest.mark.parametrize("args", [{}, {"source": "cmd\n", "path": "cmd.script"}])
    async def test_run_requires_exactly_one_source_or_path(self, connected_entry, script_env, args):
        state, conn = connected_entry

        result = await handle_script_run(state, {**SID, **args, "log": False})

        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()

    async def test_inline_source_is_limited_to_256_kibibytes(self, connected_entry, script_env):
        state, conn = connected_entry

        result = await handle_script_run(
            state,
            {**SID, "source": "x" * (256 * 1024 + 1), "log": False},
        )

        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()

    @pytest.mark.parametrize("wait_ms", [-1, 60001, "bad"])
    async def test_wait_ms_rejects_values_outside_its_integer_range(
        self, connected_entry, script_env, wait_ms
    ):
        state, conn = connected_entry

        result = await handle_script_run(
            state,
            {**SID, "source": "cmd\n", "wait_ms": wait_ms, "log": False},
        )

        assert result["error"]["code"] == "invalid_params"
        conn.ser.write.assert_not_called()

    async def test_wait_ms_returns_full_result_and_status_keeps_it(self, connected_entry, script_env):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        source = "@prompt --off\nfirst\nsecond\n"

        run = await handle_script_run(
            state,
            {**SID, "source": source, "wait_ms": "5000", "log": False},
        )
        later = await handle_script_status(state, {"run_id": run["run_id"]})
        latest = await handle_script_status(state, SID)

        assert run["ok"]
        assert run["state"] == "passed"
        assert run["result"]["state"] == "passed"
        assert len(run["result"]["steps"]) == 3
        assert later["result"] == run["result"]
        assert latest["run_id"] == run["run_id"]
        assert latest["result"] == run["result"]
        assert later["completed_steps"] == 3
        assert b"first\n" in conn.ser.write.call_args_list[0].args[0]

    async def test_started_run_owns_connection_and_allows_reads_and_marks(self, connected_entry, script_env):
        state, _conn = connected_entry
        await handle_log_start(state, {**SID, "log_formats": ["jsonl"]})
        run_id = await _running_script(state)

        rejected = [
            await handle_write(state, {**SID, "data": "outside"}),
            await handle_send_file(state, {**SID, "path": "missing"}),
            await handle_configure(state, {**SID, "newline": "\\r\\n"}),
            await handle_flush(state, SID),
            await handle_set_dtr(state, {**SID, "value": "false"}),
            await handle_set_rts(state, {**SID, "value": "false"}),
            await handle_pulse_dtr(state, {**SID, "duration_ms": "0"}),
            await handle_pulse_rts(state, {**SID, "duration_ms": "0"}),
            await handle_log_start(state, SID),
            await handle_log_stop(state, SID),
        ]
        read = await handle_read(state, {**SID, "timeout_ms": "0"})
        connection_status = await handle_connection_status(state, SID)
        mark = await handle_log_mark(state, {**SID, "label": "still available"})
        log_status = await handle_log_status(state, SID)
        plugin_write = await handle_write(state, {**SID, "data": "plugin"})
        await handle_script_cancel(state, {"run_id": run_id})
        await handle_log_stop(state, SID)

        assert all(result["error"]["code"] == "script_running" for result in rejected)
        assert all(run_id in result["error"]["message"] for result in rejected)
        assert read["ok"] and connection_status["ok"] and mark["ok"] and log_status["ok"]
        assert plugin_write["error"]["code"] == "script_running"

    async def test_claims_are_per_connection_and_duplicate_runs_are_rejected(
        self, connected_entry, another_connection, script_env
    ):
        state, _conn = connected_entry
        first_id = await _running_script(state)

        duplicate = await handle_script_run(state, {**SID, "source": "other\n", "log": False})
        other = await handle_script_run(
            state, {"connection_id": "s2", "source": "@sleep 60s\n", "log": False}
        )
        await handle_script_cancel(state, {"run_id": first_id})
        await handle_script_cancel(state, {"run_id": other["run_id"]})

        assert duplicate["error"]["code"] == "script_running"
        assert first_id in duplicate["error"]["message"]
        assert other["ok"]

    async def test_connection_session_writes_mcp_source_files_and_control_lines(
        self, connected_entry, script_env
    ):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        files = Path(os.environ["SERIAL_MCP_FILES_DIR"])
        (files / "boot.txt").write_bytes(b"boot\x00")

        result = await handle_script_run(
            state,
            {
                **SID,
                "source": "@prompt --off\nhello\n@dtr on\n@send_file boot.txt\n",
                "wait_ms": "5000",
                "log_formats": ["jsonl"],
            },
        )

        assert result["state"] == "passed"
        assert [call.args[0] for call in conn.ser.write.call_args_list] == [b"hello\n", b"boot\x00"]
        assert conn.ser.dtr is True
        assert result["result"]["log_paths"]
        assert result["result"]["steps"][-1]["text"] == "@send_file boot.txt"
        conn.reader.subscribe.assert_called_once()

    async def test_live_status_counts_completed_steps_without_exposing_variable_values(
        self, connected_entry, script_env
    ):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        run = await handle_script_run(
            state,
            {
                **SID,
                "source": "{{DEVICE}}\n@sleep 60s\n",
                "vars": {"DEVICE": "secret-command"},
                "log": False,
            },
        )
        status = await _wait_for_status(
            state,
            run["run_id"],
            lambda value: value.get("current_line") == 2 and value.get("completed_steps") == 1,
        )
        await handle_script_cancel(state, {"run_id": run["run_id"]})
        result = await handle_script_status(state, {"run_id": run["run_id"]})

        assert status["current_text"] == "@sleep 60s"
        assert status["elapsed_seconds"] > 0
        assert result["result"]["variables"] == ["DEVICE"]
        assert result["result"]["steps"][0]["text"] == "{{DEVICE}}"
        assert "secret-command" not in str(result["result"])
        assert conn.ser.write.call_args.args[0] == b"secret-command\n"

    async def test_runner_failure_and_start_exception_both_release_the_claim(
        self, connected_entry, script_env, monkeypatch
    ):
        state, conn = connected_entry
        conn.ser.write.side_effect = len

        def fail_step(_runner, _execution, _settings):
            raise RuntimeError("runner test failure")

        with monkeypatch.context() as patch:
            patch.setattr(ScriptRunner, "_execute_steps", fail_step)
            failed = await handle_script_run(
                state,
                {**SID, "source": "@sleep 1ms\n", "wait_ms": "5000", "log": False},
            )

        assert failed["result"]["state"] == "errored"
        recovered = await handle_script_run(
            state,
            {**SID, "source": "@prompt --off\nnext\n", "wait_ms": "5000", "log": False},
        )
        assert recovered["state"] == "passed"

        def fail_start(_runner):
            raise RuntimeError("runner start failure")

        with monkeypatch.context() as patch:
            patch.setattr(ScriptRunner, "start", fail_start)
            with pytest.raises(RuntimeError, match="runner start failure"):
                await handle_script_run(
                    state,
                    {**SID, "source": "@sleep 1ms\n", "log": False},
                )

        after_exception = await handle_script_run(
            state,
            {**SID, "source": "@prompt --off\nlast\n", "wait_ms": "5000", "log": False},
        )
        assert after_exception["state"] == "passed"

    async def test_cancel_cancels_a_live_run_and_releases_its_claim(self, connected_entry, script_env):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        run_id = await _running_script(state)

        cancelled = await handle_script_cancel(state, {"run_id": run_id})
        latest = await handle_script_status(state, {"run_id": run_id})
        next_run = await handle_script_run(
            state,
            {**SID, "source": "@prompt --off\nfree\n", "wait_ms": "5000", "log": False},
        )

        assert cancelled["state"] == "cancelled"
        assert latest["state"] == "cancelled"
        assert next_run["state"] == "passed"

    async def test_cancel_returns_requested_after_five_seconds_and_keeps_claim_until_exit(
        self, connected_entry, script_env, monkeypatch
    ):
        state, _conn = connected_entry
        run_id = await _running_script(state)
        job = state.script_jobs.active_for_connection("s1")
        assert job is not None
        original_cancel = ScriptRunner.cancel
        original_join = ScriptRunner.join

        def ignore_cancel(_runner):
            return None

        def skip_bounded_wait(runner, timeout=None):
            if timeout is not None:
                return None
            return original_join(runner, timeout)

        monkeypatch.setattr(ScriptRunner, "cancel", ignore_cancel)
        monkeypatch.setattr(ScriptRunner, "join", skip_bounded_wait)
        timed_out = await handle_script_cancel(state, {"run_id": run_id})
        duplicate = await handle_script_run(state, {**SID, "source": "other\n", "log": False})

        original_cancel(job.runner)
        assert await asyncio.to_thread(job.runner.done.wait, 2.0)
        released = await _wait_for_status(state, run_id, lambda value: value["state"] == "cancelled")

        assert timed_out["cancel_requested"] is True
        assert duplicate["error"]["code"] == "script_running"
        assert released["state"] == "cancelled"

    async def test_close_cancels_before_stopping_the_connection(self, connected_entry, script_env):
        state, _conn = connected_entry
        run_id = await _running_script(state)

        closed = await handle_close(state, SID)
        status = await handle_script_status(state, {"run_id": run_id})

        assert closed["ok"]
        assert status["state"] == "cancelled"
        assert "s1" not in state.connections
        assert state.script_jobs.active_for_connection("s1") is None

    async def test_shutdown_cancels_without_waiting_for_the_script(self, connected_entry, script_env):
        state, _conn = connected_entry
        run_id = await _running_script(state)
        start = time.monotonic()

        await state.shutdown()

        elapsed = time.monotonic() - start
        status = await _wait_for_status(state, run_id, lambda value: value["state"] == "cancelled")
        assert elapsed < 0.25
        assert status["state"] == "cancelled"

    async def test_default_logging_starts_and_stops_and_custom_formats_are_used(
        self, connected_entry, script_env
    ):
        state, conn = connected_entry
        conn.ser.write.side_effect = len

        default = await handle_script_run(
            state,
            {**SID, "source": "@prompt --off\nfirst\n", "wait_ms": "5000"},
        )
        assert default["state"] == "passed"
        assert [Path(path).suffix for path in default["result"]["log_paths"]] == [".txt"]
        assert conn.log_slot.log is None

        custom = await handle_script_run(
            state,
            {
                **SID,
                "source": "@prompt --off\nsecond\n",
                "wait_ms": "5000",
                "log_formats": ["jsonl", "raw"],
            },
        )
        assert custom["state"] == "passed"
        assert {Path(path).suffix for path in custom["result"]["log_paths"]} == {".jsonl", ".bin"}
        assert conn.log_slot.log is None

    async def test_an_existing_port_log_is_left_running(self, connected_entry, script_env):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        active = await handle_log_start(state, {**SID, "log_formats": ["jsonl"]})

        run = await handle_script_run(
            state,
            {**SID, "source": "@prompt --off\nrun\n", "wait_ms": "5000"},
        )
        status = await handle_log_status(state, SID)
        await handle_log_stop(state, SID)

        assert active["ok"]
        assert run["state"] == "passed"
        assert status["logging"] is True
        assert conn.log_slot.log is None
        assert run["result"]["log_paths"] == [active["log"]["files"]["jsonl"]]

    async def test_finished_result_registry_keeps_the_last_twenty_runs(self, connected_entry, script_env):
        state, _conn = connected_entry
        run_ids = []
        for _ in range(21):
            result = await handle_script_run(
                state,
                {**SID, "source": "@prompt --off\n", "wait_ms": "5000", "log": False},
            )
            run_ids.append(result["run_id"])

        evicted = await handle_script_status(state, {"run_id": run_ids[0]})
        retained = await handle_script_status(state, {"run_id": run_ids[-1]})

        assert evicted["error"]["code"] == "not_found"
        assert retained["state"] == "passed"


class TestScriptDirectory:
    async def test_run_reads_a_confined_script_file(self, connected_entry, script_env):
        state, conn = connected_entry
        conn.ser.write.side_effect = len
        (script_env / "boot.script").write_text("@prompt --off\nboot-version\n", encoding="utf-8")

        run = await handle_script_run(
            state,
            {**SID, "path": "boot.script", "wait_ms": "5000", "log": "false"},
        )

        assert run["state"] == "passed"
        assert run["result"]["script"] == "boot.script"
        assert conn.ser.write.call_args.args[0] == b"boot-version\n"

    @pytest.mark.skipif(os.name == "nt", reason="FIFO paths require POSIX")
    async def test_path_is_confined_and_requires_a_regular_file(self, connected_entry, script_env, tmp_path):
        state, conn = connected_entry
        (script_env / "good.script").write_text("@prompt --off\nhello\n", encoding="utf-8")
        outside = tmp_path / "outside.script"
        outside.write_text("hello\n", encoding="utf-8")
        os.mkfifo(script_env / "pipe")
        paths = ["../outside.script", str(outside), "pipe"]

        results = [await handle_script_run(state, {**SID, "path": path, "log": False}) for path in paths]

        assert all(result["error"]["code"] == "invalid_params" for result in results)
        conn.ser.write.assert_not_called()

    async def test_list_reports_size_and_first_comment_line(self, connected_entry, script_env):
        state, _conn = connected_entry
        content = "# boot smoke test\n@prompt --off\nversion\n"
        (script_env / "boot.script").write_text(content, encoding="utf-8")

        result = await handle_script_list(state, {})

        assert result["ok"]
        assert result["scripts"] == [
            {"name": "boot.script", "size": len(content.encode()), "description": "boot smoke test"}
        ]

    async def test_scripts_directory_environment_is_read_when_tool_is_called(
        self, connected_entry, script_env, monkeypatch, tmp_path
    ):
        state, _conn = connected_entry
        other = tmp_path / "other-scripts"
        other.mkdir()
        (other / "other.script").write_text("# other\n", encoding="utf-8")
        monkeypatch.setenv("SERIAL_MCP_SCRIPTS_DIR", str(other))

        result = await handle_script_list(state, {})

        assert [script["name"] for script in result["scripts"]] == ["other.script"]

    async def test_template_includes_directive_reference_skeleton_and_echo_warning(self, serial_state):
        result = await handle_script_template(serial_state, {})
        template = result["template"]

        assert result["ok"]
        for name, directive in DIRECTIVES.items():
            assert name in template
            assert directive.usage in template
        assert "echo" in template.lower()
        assert "@prompt" in template
        assert "@send" in template
