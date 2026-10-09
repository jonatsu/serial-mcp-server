"""Tests for serial_mcp_server.mcp.tools.spec — the spec tool handlers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.writer import SerialWriter
from serial_mcp_server.specs.store import (
    register_spec,
)
from tests.specs.samples import VALID_SPEC, data_root_for, settings_for, write_spec

# ---------------------------------------------------------------------------
# Spec handler tests
# ---------------------------------------------------------------------------


class TestSpecHandlers:
    """Tests for handler functions in serial_mcp_server.mcp.tools.spec."""

    async def test_template_returns_template_and_path(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_template

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_template(state, {})
        assert result["ok"] is True
        assert "kind: serial-protocol" in result["template"]
        assert "suggested_path" in result

    async def test_template_with_device_name(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_template

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_template(state, {"device_name": "SensorTag"})
        assert result["ok"] is True
        assert "SensorTag" in result["template"]
        assert "sensortag" in result["suggested_path"]

    async def test_register_success(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_register

        spec_file = write_spec(tmp_path, VALID_SPEC)
        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_register(state, {"path": str(spec_file)})
        assert result["ok"] is True
        assert result["name"] == "Test Device"
        assert result["spec_id"]

    async def test_register_file_not_found(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_register

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_register(state, {"path": str(tmp_path / "nope.md")})
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"

    async def test_register_invalid_frontmatter(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_register

        bad_spec = "---\nkind: wrong\n---\n# Bad\n"
        spec_file = write_spec(tmp_path, bad_spec)
        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_register(state, {"path": str(spec_file)})
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_spec"

    async def test_list_empty(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_list

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_list(state, {})
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["specs"] == []

    async def test_list_with_registered(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_list

        spec_file = write_spec(tmp_path, VALID_SPEC)
        register_spec(data_root_for(tmp_path), spec_file)

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_list(state, {})
        assert result["ok"] is True
        assert result["count"] == 1

    async def test_attach_success(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_attach

        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry_data = register_spec(data_root_for(tmp_path), spec_file)

        state = SerialState(settings_for(tmp_path))
        conn = SerialConnection(
            connection_id="c1",
            port="/dev/ttyUSB0",
            baudrate=115200,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.2,
            write_timeout=0.2,
            encoding="utf-8",
            newline="\n",
            ser=MagicMock(),
            writer=SerialWriter(MagicMock()),
        )
        state.connections["c1"] = conn

        result = await handle_spec_attach(
            state,
            {
                "connection_id": "c1",
                "spec_id": entry_data["spec_id"],
            },
        )
        assert result["ok"] is True
        assert result["spec_id"] == entry_data["spec_id"]
        assert conn.spec is not None
        assert conn.spec["spec_id"] == entry_data["spec_id"]

    async def test_attach_unknown_spec_id(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_attach

        state = SerialState(settings_for(tmp_path))
        conn = SerialConnection(
            connection_id="c1",
            port="/dev/ttyUSB0",
            baudrate=115200,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.2,
            write_timeout=0.2,
            encoding="utf-8",
            newline="\n",
            ser=MagicMock(),
            writer=SerialWriter(MagicMock()),
        )
        state.connections["c1"] = conn

        result = await handle_spec_attach(
            state,
            {
                "connection_id": "c1",
                "spec_id": "nonexistent",
            },
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"

    async def test_attach_unknown_connection_id(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_attach

        state = SerialState(settings_for(tmp_path))
        with pytest.raises(KeyError, match="Unknown connection_id"):
            await handle_spec_attach(
                state,
                {
                    "connection_id": "nope",
                    "spec_id": "anything",
                },
            )

    async def test_get_returns_attached_spec(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_get

        state = SerialState(settings_for(tmp_path))
        conn = SerialConnection(
            connection_id="c1",
            port="/dev/ttyUSB0",
            baudrate=115200,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.2,
            write_timeout=0.2,
            encoding="utf-8",
            newline="\n",
            ser=MagicMock(),
            writer=SerialWriter(MagicMock()),
        )
        conn.spec = {"spec_id": "test123", "path": "/tmp/test.md", "meta": {}}
        state.connections["c1"] = conn

        result = await handle_spec_get(state, {"connection_id": "c1"})
        assert result["ok"] is True
        assert result["spec"]["spec_id"] == "test123"

    async def test_get_returns_none_when_no_spec(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_get

        state = SerialState(settings_for(tmp_path))
        conn = SerialConnection(
            connection_id="c1",
            port="/dev/ttyUSB0",
            baudrate=115200,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.2,
            write_timeout=0.2,
            encoding="utf-8",
            newline="\n",
            ser=MagicMock(),
            writer=SerialWriter(MagicMock()),
        )
        state.connections["c1"] = conn

        result = await handle_spec_get(state, {"connection_id": "c1"})
        assert result["ok"] is True
        assert result["spec"] is None

    async def test_read_success(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_read

        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry_data = register_spec(data_root_for(tmp_path), spec_file)

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_read(state, {"spec_id": entry_data["spec_id"]})
        assert result["ok"] is True
        assert result["spec_id"] == entry_data["spec_id"]
        assert "# Test Device Protocol" in result["content"]

    async def test_read_unknown_spec_id(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_read

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_read(state, {"spec_id": "nonexistent"})
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"

    async def test_search_returns_matches(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_search

        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry_data = register_spec(data_root_for(tmp_path), spec_file)

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_search(
            state,
            {
                "spec_id": entry_data["spec_id"],
                "query": "sensor",
            },
        )
        assert result["ok"] is True
        assert result["count"] > 0
        assert len(result["results"]) > 0

    async def test_search_no_matches(self, monkeypatch, tmp_path):
        from serial_mcp_server.mcp.tools.spec import handle_spec_search

        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry_data = register_spec(data_root_for(tmp_path), spec_file)

        state = SerialState(settings_for(tmp_path))
        result = await handle_spec_search(
            state,
            {
                "spec_id": entry_data["spec_id"],
                "query": "zzzznonexistent",
            },
        )
        assert result["ok"] is True
        assert result["count"] == 0
