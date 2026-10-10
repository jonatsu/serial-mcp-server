"""Tests for serial_mcp_server.mcp.trace — no serial hardware required."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from serial_mcp_server import config
from serial_mcp_server.mcp.trace import TraceBuffer, get_trace_buffer, init_trace, sanitize_args

# ---------------------------------------------------------------------------
# TraceBuffer basics
# ---------------------------------------------------------------------------


class TestTraceBuffer:
    def test_emit_and_tail(self):
        buf = TraceBuffer()
        buf.emit({"event": "test", "data": 1})
        buf.emit({"event": "test", "data": 2})
        events = buf.tail()
        assert len(events) == 2
        assert events[0]["data"] == 1
        assert events[1]["data"] == 2
        # Each event gets a timestamp
        assert "ts" in events[0]
        assert "ts" in events[1]

    def test_tail_limit(self):
        buf = TraceBuffer()
        for i in range(10):
            buf.emit({"event": "test", "data": i})
        events = buf.tail(3)
        assert len(events) == 3
        assert events[0]["data"] == 7
        assert events[2]["data"] == 9

    def test_status(self):
        buf = TraceBuffer()
        buf.emit({"event": "test"})
        status = buf.status()
        assert status["enabled"] is True
        assert status["event_count"] == 1
        assert status["file_path"] is None

    def test_ring_buffer_eviction(self):
        buf = TraceBuffer(max_items=5)
        for i in range(10):
            buf.emit({"event": "test", "data": i})
        events = buf.tail(10)
        assert len(events) == 5
        assert events[0]["data"] == 5
        assert events[4]["data"] == 9

    def test_rejects_symlink_path(self, tmp_path: Path):
        real_file = tmp_path / "real.jsonl"
        real_file.touch()
        link = tmp_path / "link.jsonl"
        link.symlink_to(real_file)
        # On POSIX: os.open with O_NOFOLLOW raises OSError
        # On Windows (no O_NOFOLLOW): falls back to check-then-open raising ValueError
        with pytest.raises((OSError, ValueError)):
            TraceBuffer(file_path=str(link))

    def test_file_sink(self, tmp_path: Path):
        trace_file = tmp_path / "trace.jsonl"
        buf = TraceBuffer(file_path=str(trace_file))
        buf.emit({"event": "test", "data": "hello"})
        buf.emit({"event": "test", "data": "world"})
        buf.close()

        lines = trace_file.read_text().strip().splitlines()
        assert len(lines) == 2
        parsed = json.loads(lines[0])
        assert parsed["event"] == "test"
        assert parsed["data"] == "hello"
        assert "ts" in parsed
        parsed2 = json.loads(lines[1])
        assert parsed2["data"] == "world"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    @pytest.mark.parametrize("existing_mode", [None, 0o644])
    def test_the_trace_file_is_readable_by_its_owner_only(self, tmp_path: Path, existing_mode):
        trace_file = tmp_path / "trace.jsonl"
        if existing_mode is not None:  # left by an earlier run with the old mode
            trace_file.touch()
            trace_file.chmod(existing_mode)

        TraceBuffer(file_path=str(trace_file)).close()

        assert stat.S_IMODE(trace_file.stat().st_mode) == 0o600

    def test_close_idempotent(self, tmp_path: Path):
        trace_file = tmp_path / "trace.jsonl"
        buf = TraceBuffer(file_path=str(trace_file))
        buf.close()
        buf.close()  # Should not raise


# ---------------------------------------------------------------------------
# sanitize_args
# ---------------------------------------------------------------------------


class TestSanitizeArgs:
    def test_strips_payloads_when_disabled(self):
        args = {"connection_id": "c1", "data": "hello world", "as": "text"}
        result = sanitize_args(args)
        assert "data" not in result
        assert result["connection_id"] == "c1"
        assert result["as"] == "text"

    def test_passes_through_non_payload_args(self):
        args = {"connection_id": "c1", "timeout_ms": 500}
        result = sanitize_args(args)
        assert result == args

    def test_truncates_when_payloads_enabled(self):
        args = {"data": "A" * 20}
        result = sanitize_args(args, payloads=True, max_bytes=10)
        assert result["data"] == "A" * 10 + "\u2026"

    def test_no_truncation_when_under_limit(self):
        args = {"data": "0102"}
        result = sanitize_args(args, payloads=True, max_bytes=100)
        assert result["data"] == "0102"

    def test_deep_copy(self):
        args = {"connection_id": "c1", "nested": {"a": 1}}
        result = sanitize_args(args)
        result["nested"]["a"] = 999
        assert args["nested"]["a"] == 1  # Original not mutated

    def test_script_source_and_variables_are_stripped_by_default(self):
        args = {
            "connection_id": "s1",
            "source": "@sendline login admin\nhunter2\n",
            "vars": {"PASSWORD": "hunter2"},
            "path": "uboot.serial",
        }

        result = sanitize_args(args)

        assert result == {"connection_id": "s1", "path": "uboot.serial"}

    def test_script_source_and_variables_are_kept_and_truncated_when_payloads_are_enabled(self):
        short = sanitize_args({"source": "@sleep 1s", "vars": {"A": "b"}}, payloads=True, max_bytes=20)
        long = sanitize_args(
            {"source": "x" * 40, "vars": {"PASSWORD": "p" * 40}}, payloads=True, max_bytes=20
        )

        assert short == {"source": "@sleep 1s", "vars": {"A": "b"}}
        assert long["source"] == "x" * 20 + "\u2026"
        assert isinstance(long["vars"], str)
        assert long["vars"] == json.dumps({"PASSWORD": "p" * 40})[:20] + "\u2026"


# ---------------------------------------------------------------------------
# init_trace
# ---------------------------------------------------------------------------


class TestInitTrace:
    @pytest.fixture(autouse=True)
    def no_buffer_left_behind(self, monkeypatch):
        monkeypatch.setattr("serial_mcp_server.mcp.trace._buffer", None)
        yield
        buf = get_trace_buffer()
        if buf is not None:
            buf.close()

    def test_returns_none_when_disabled(self, tmp_path: Path):
        settings = config.load({"SERIAL_MCP_SPEC_ROOT": str(tmp_path), "SERIAL_MCP_TRACE": "off"})

        assert init_trace(settings) is None

    def test_disabling_replaces_an_earlier_buffer(self, tmp_path: Path):
        init_trace(config.load({"SERIAL_MCP_SPEC_ROOT": str(tmp_path)}))

        init_trace(config.load({"SERIAL_MCP_SPEC_ROOT": str(tmp_path), "SERIAL_MCP_TRACE": "0"}))

        assert get_trace_buffer() is None

    def test_creates_buffer_when_enabled(self, tmp_path: Path):
        spec_root = tmp_path / ".serial_mcp"
        settings = config.load(
            {
                "SERIAL_MCP_SPEC_ROOT": str(spec_root),
                "SERIAL_MCP_TRACE_PAYLOADS": "yes",
                "SERIAL_MCP_TRACE_MAX_BYTES": "7",
            }
        )

        buf = init_trace(settings)

        assert buf is not None
        assert buf is get_trace_buffer()
        assert (spec_root / "traces").is_dir()
        assert buf.status()["payloads_logged"] is True
        assert buf.status()["max_payload_bytes"] == 7
        assert buf.sanitize({"data": "x" * 10}) == {"data": "x" * 7 + "\u2026"}


# ---------------------------------------------------------------------------
# Handler returns enabled=False when tracing off
# ---------------------------------------------------------------------------


class TestHandlersTraceDisabled:
    async def test_status_disabled(self, monkeypatch, serial_state):
        monkeypatch.setattr("serial_mcp_server.mcp.trace._buffer", None)
        from serial_mcp_server.mcp.tools.trace import handle_trace_status

        result = await handle_trace_status(serial_state, {})
        assert result["ok"] is True
        assert result["enabled"] is False

    async def test_tail_disabled(self, monkeypatch, serial_state):
        monkeypatch.setattr("serial_mcp_server.mcp.trace._buffer", None)
        from serial_mcp_server.mcp.tools.trace import handle_trace_tail

        result = await handle_trace_tail(serial_state, {})
        assert result["ok"] is True
        assert result["enabled"] is False


class TestHandlersTraceEnabled:
    async def test_status_enabled(self, monkeypatch, serial_state):
        buf = TraceBuffer()
        buf.emit({"event": "test"})
        monkeypatch.setattr("serial_mcp_server.mcp.trace._buffer", buf)
        from serial_mcp_server.mcp.tools.trace import handle_trace_status

        result = await handle_trace_status(serial_state, {})
        assert result["ok"] is True
        assert result["enabled"] is True
        assert result["event_count"] == 1

    async def test_tail_enabled(self, monkeypatch, serial_state):
        buf = TraceBuffer()
        buf.emit({"event": "test", "data": 42})
        monkeypatch.setattr("serial_mcp_server.mcp.trace._buffer", buf)
        from serial_mcp_server.mcp.tools.trace import handle_trace_tail

        result = await handle_trace_tail(serial_state, {"n": 10})
        assert result["ok"] is True
        assert len(result["events"]) == 1
        assert result["events"][0]["data"] == 42
