"""Tests for serial_mcp_server.specs.store — the spec index and the spec tool handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serial_mcp_server import config
from serial_mcp_server.config import Settings
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.writer import SerialWriter
from serial_mcp_server.specs.store import (
    get_template,
    list_specs,
    read_spec,
    register_spec,
    search_spec,
)
from serial_mcp_server.state import SerialState
from tests.specs.samples import MINIMAL_SPEC, VALID_SPEC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_spec(tmp_path: Path, content: str, name: str = "test-device.md") -> Path:
    """Write a spec file and return its path."""
    spec_dir = tmp_path / ".serial_mcp" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_file = spec_dir / name
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


def _spec_root(tmp_path: Path) -> Path:
    """The data root these tests use: tmp_path/.serial_mcp."""
    return tmp_path / ".serial_mcp"


def _settings(tmp_path: Path) -> Settings:
    return config.load({"SERIAL_MCP_SPEC_ROOT": str(_spec_root(tmp_path))})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegisterSpec:
    def test_valid(self, monkeypatch, tmp_path):
        spec_file = _write_spec(tmp_path, VALID_SPEC)
        result = register_spec(_spec_root(tmp_path), spec_file)
        assert result["name"] == "Test Device"
        assert result["spec_id"]

    def test_missing_file(self, monkeypatch, tmp_path):
        with pytest.raises(FileNotFoundError):
            register_spec(_spec_root(tmp_path), tmp_path / "nonexistent.md")

    def test_invalid_frontmatter(self, monkeypatch, tmp_path):
        bad_spec = "---\nkind: wrong\n---\n# Bad\n"
        spec_file = _write_spec(tmp_path, bad_spec)
        with pytest.raises(ValueError, match="Invalid spec front-matter"):
            register_spec(_spec_root(tmp_path), spec_file)

    def test_idempotent(self, monkeypatch, tmp_path):
        spec_file = _write_spec(tmp_path, VALID_SPEC)
        r1 = register_spec(_spec_root(tmp_path), spec_file)
        r2 = register_spec(_spec_root(tmp_path), spec_file)
        assert r1["spec_id"] == r2["spec_id"]

    def test_rejects_path_outside_project(self, monkeypatch, tmp_path):
        outside = tmp_path.parent / "outside.md"
        outside.write_text(VALID_SPEC, encoding="utf-8")
        with pytest.raises(ValueError, match="must be inside the project"):
            register_spec(_spec_root(tmp_path), outside)

    def test_rejects_traversal_path(self, monkeypatch, tmp_path):
        outside = tmp_path.parent / "sneaky.md"
        outside.write_text(VALID_SPEC, encoding="utf-8")
        traversal = tmp_path / ".serial_mcp" / "specs" / ".." / ".." / ".." / "sneaky.md"
        with pytest.raises(ValueError, match="must be inside the project"):
            register_spec(_spec_root(tmp_path), traversal)

    def test_allows_path_inside_project(self, monkeypatch, tmp_path):
        # Spec in a subdirectory of the project (not in .serial_mcp/specs/)
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        spec_file = docs_dir / "my-device.md"
        spec_file.write_text(VALID_SPEC, encoding="utf-8")
        result = register_spec(_spec_root(tmp_path), spec_file)
        assert result["name"] == "Test Device"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestListSpecs:
    def test_empty(self, monkeypatch, tmp_path):
        assert list_specs(_spec_root(tmp_path)) == []

    def test_lists_registered(self, monkeypatch, tmp_path):
        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(_spec_root(tmp_path), spec_file)

        result = list_specs(_spec_root(tmp_path))
        assert len(result) == 1
        assert result[0]["spec_id"] == entry["spec_id"]
        assert result[0]["name"] == "Test Device"

    def test_multiple(self, monkeypatch, tmp_path):
        _write_spec(tmp_path, VALID_SPEC, name="a.md")
        _write_spec(tmp_path, MINIMAL_SPEC, name="b.md")
        register_spec(_spec_root(tmp_path), tmp_path / ".serial_mcp" / "specs" / "a.md")
        register_spec(_spec_root(tmp_path), tmp_path / ".serial_mcp" / "specs" / "b.md")

        result = list_specs(_spec_root(tmp_path))
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Read spec
# ---------------------------------------------------------------------------


class TestReadSpec:
    def test_read(self, monkeypatch, tmp_path):
        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(_spec_root(tmp_path), spec_file)

        result = read_spec(_spec_root(tmp_path), entry["spec_id"])
        assert result["spec_id"] == entry["spec_id"]
        assert result["meta"]["name"] == "Test Device"
        assert "# Test Device Protocol" in result["body"]
        assert result["content"] == VALID_SPEC

    def test_unknown_id(self, monkeypatch, tmp_path):
        with pytest.raises(KeyError):
            read_spec(_spec_root(tmp_path), "nonexistent0000")

    def test_rejects_path_outside_project_in_index(self, monkeypatch, tmp_path):
        """If index.json is tampered to point outside the project, read_spec refuses."""
        import json

        spec_root = _spec_root(tmp_path)
        spec_root.mkdir(parents=True, exist_ok=True)
        # Write a tampered index pointing to a file outside the project
        index_path = spec_root / "index.json"
        index_path.write_text(
            json.dumps(
                {"bad": {"spec_id": "bad", "path": "/etc/passwd", "name": "Evil", "kind": "serial-protocol"}}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="outside the project"):
            read_spec(_spec_root(tmp_path), "bad")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearchSpec:
    def test_basic(self, monkeypatch, tmp_path):
        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(_spec_root(tmp_path), spec_file)

        results = search_spec(_spec_root(tmp_path), entry["spec_id"], "sensor")
        assert len(results) > 0
        assert results[0]["line"] > 0
        assert "sensor" in results[0]["text"].lower()

    def test_line_numbers(self, monkeypatch, tmp_path):
        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(_spec_root(tmp_path), spec_file)

        results = search_spec(_spec_root(tmp_path), entry["spec_id"], "Commands")
        assert len(results) > 0
        for r in results:
            assert isinstance(r["line"], int)
            assert r["line"] > 0

    def test_k_limit(self, monkeypatch, tmp_path):
        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(_spec_root(tmp_path), spec_file)

        results = search_spec(_spec_root(tmp_path), entry["spec_id"], "a", k=2)
        assert len(results) <= 2

    def test_context(self, monkeypatch, tmp_path):
        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(_spec_root(tmp_path), spec_file)

        results = search_spec(_spec_root(tmp_path), entry["spec_id"], "sensor")
        assert len(results) > 0
        assert "context" in results[0]
        # Context should contain line numbers
        assert ":" in results[0]["context"]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


class TestGetTemplate:
    def test_default(self):
        template = get_template()
        assert "kind: serial-protocol" in template
        assert "My Device" in template

    def test_with_device_name(self):
        template = get_template("SensorTag")
        assert "SensorTag" in template
        assert 'name: "SensorTag Protocol"' in template


# ---------------------------------------------------------------------------
# Spec handler tests
# ---------------------------------------------------------------------------


class TestSpecHandlers:
    """Tests for handler functions in serial_mcp_server.handlers_spec."""

    async def test_template_returns_template_and_path(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_template

        state = SerialState(_settings(tmp_path))
        result = await handle_spec_template(state, {})
        assert result["ok"] is True
        assert "kind: serial-protocol" in result["template"]
        assert "suggested_path" in result

    async def test_template_with_device_name(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_template

        state = SerialState(_settings(tmp_path))
        result = await handle_spec_template(state, {"device_name": "SensorTag"})
        assert result["ok"] is True
        assert "SensorTag" in result["template"]
        assert "sensortag" in result["suggested_path"]

    async def test_register_success(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_register

        spec_file = _write_spec(tmp_path, VALID_SPEC)
        state = SerialState(_settings(tmp_path))
        result = await handle_spec_register(state, {"path": str(spec_file)})
        assert result["ok"] is True
        assert result["name"] == "Test Device"
        assert result["spec_id"]

    async def test_register_file_not_found(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_register

        state = SerialState(_settings(tmp_path))
        result = await handle_spec_register(state, {"path": str(tmp_path / "nope.md")})
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"

    async def test_register_invalid_frontmatter(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_register

        bad_spec = "---\nkind: wrong\n---\n# Bad\n"
        spec_file = _write_spec(tmp_path, bad_spec)
        state = SerialState(_settings(tmp_path))
        result = await handle_spec_register(state, {"path": str(spec_file)})
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_spec"

    async def test_list_empty(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_list

        state = SerialState(_settings(tmp_path))
        result = await handle_spec_list(state, {})
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["specs"] == []

    async def test_list_with_registered(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_list

        spec_file = _write_spec(tmp_path, VALID_SPEC)
        register_spec(_spec_root(tmp_path), spec_file)

        state = SerialState(_settings(tmp_path))
        result = await handle_spec_list(state, {})
        assert result["ok"] is True
        assert result["count"] == 1

    async def test_attach_success(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_attach

        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry_data = register_spec(_spec_root(tmp_path), spec_file)

        state = SerialState(_settings(tmp_path))
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
        from serial_mcp_server.handlers_spec import handle_spec_attach

        state = SerialState(_settings(tmp_path))
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
        from serial_mcp_server.handlers_spec import handle_spec_attach

        state = SerialState(_settings(tmp_path))
        with pytest.raises(KeyError, match="Unknown connection_id"):
            await handle_spec_attach(
                state,
                {
                    "connection_id": "nope",
                    "spec_id": "anything",
                },
            )

    async def test_get_returns_attached_spec(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_get

        state = SerialState(_settings(tmp_path))
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
        from serial_mcp_server.handlers_spec import handle_spec_get

        state = SerialState(_settings(tmp_path))
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
        from serial_mcp_server.handlers_spec import handle_spec_read

        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry_data = register_spec(_spec_root(tmp_path), spec_file)

        state = SerialState(_settings(tmp_path))
        result = await handle_spec_read(state, {"spec_id": entry_data["spec_id"]})
        assert result["ok"] is True
        assert result["spec_id"] == entry_data["spec_id"]
        assert "# Test Device Protocol" in result["content"]

    async def test_read_unknown_spec_id(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_read

        state = SerialState(_settings(tmp_path))
        result = await handle_spec_read(state, {"spec_id": "nonexistent"})
        assert result["ok"] is False
        assert result["error"]["code"] == "not_found"

    async def test_search_returns_matches(self, monkeypatch, tmp_path):
        from serial_mcp_server.handlers_spec import handle_spec_search

        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry_data = register_spec(_spec_root(tmp_path), spec_file)

        state = SerialState(_settings(tmp_path))
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
        from serial_mcp_server.handlers_spec import handle_spec_search

        spec_file = _write_spec(tmp_path, VALID_SPEC)
        entry_data = register_spec(_spec_root(tmp_path), spec_file)

        state = SerialState(_settings(tmp_path))
        result = await handle_spec_search(
            state,
            {
                "spec_id": entry_data["spec_id"],
                "query": "zzzznonexistent",
            },
        )
        assert result["ok"] is True
        assert result["count"] == 0
