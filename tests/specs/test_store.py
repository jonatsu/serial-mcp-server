"""Tests for serial_mcp_server.specs.store — the spec index and the spec tool handlers."""

from __future__ import annotations

import pytest

from serial_mcp_server.specs.store import (
    get_template,
    list_specs,
    read_spec,
    register_spec,
    search_spec,
)
from tests.specs.samples import MINIMAL_SPEC, VALID_SPEC, data_root_for, write_spec

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegisterSpec:
    def test_valid(self, monkeypatch, tmp_path):
        spec_file = write_spec(tmp_path, VALID_SPEC)
        result = register_spec(data_root_for(tmp_path), spec_file)
        assert result["name"] == "Test Device"
        assert result["spec_id"]

    def test_missing_file(self, monkeypatch, tmp_path):
        with pytest.raises(FileNotFoundError):
            register_spec(data_root_for(tmp_path), tmp_path / "nonexistent.md")

    def test_invalid_frontmatter(self, monkeypatch, tmp_path):
        bad_spec = "---\nkind: wrong\n---\n# Bad\n"
        spec_file = write_spec(tmp_path, bad_spec)
        with pytest.raises(ValueError, match="Invalid spec front-matter"):
            register_spec(data_root_for(tmp_path), spec_file)

    def test_idempotent(self, monkeypatch, tmp_path):
        spec_file = write_spec(tmp_path, VALID_SPEC)
        r1 = register_spec(data_root_for(tmp_path), spec_file)
        r2 = register_spec(data_root_for(tmp_path), spec_file)
        assert r1["spec_id"] == r2["spec_id"]

    def test_rejects_path_outside_project(self, monkeypatch, tmp_path):
        outside = tmp_path.parent / "outside.md"
        outside.write_text(VALID_SPEC, encoding="utf-8")
        with pytest.raises(ValueError, match="must be inside the project"):
            register_spec(data_root_for(tmp_path), outside)

    def test_rejects_traversal_path(self, monkeypatch, tmp_path):
        outside = tmp_path.parent / "sneaky.md"
        outside.write_text(VALID_SPEC, encoding="utf-8")
        traversal = tmp_path / ".serial_mcp" / "specs" / ".." / ".." / ".." / "sneaky.md"
        with pytest.raises(ValueError, match="must be inside the project"):
            register_spec(data_root_for(tmp_path), traversal)

    def test_allows_path_inside_project(self, monkeypatch, tmp_path):
        # Spec in a subdirectory of the project (not in .serial_mcp/specs/)
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        spec_file = docs_dir / "my-device.md"
        spec_file.write_text(VALID_SPEC, encoding="utf-8")
        result = register_spec(data_root_for(tmp_path), spec_file)
        assert result["name"] == "Test Device"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestListSpecs:
    def test_empty(self, monkeypatch, tmp_path):
        assert list_specs(data_root_for(tmp_path)) == []

    def test_lists_registered(self, monkeypatch, tmp_path):
        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(data_root_for(tmp_path), spec_file)

        result = list_specs(data_root_for(tmp_path))
        assert len(result) == 1
        assert result[0]["spec_id"] == entry["spec_id"]
        assert result[0]["name"] == "Test Device"

    def test_multiple(self, monkeypatch, tmp_path):
        write_spec(tmp_path, VALID_SPEC, name="a.md")
        write_spec(tmp_path, MINIMAL_SPEC, name="b.md")
        register_spec(data_root_for(tmp_path), tmp_path / ".serial_mcp" / "specs" / "a.md")
        register_spec(data_root_for(tmp_path), tmp_path / ".serial_mcp" / "specs" / "b.md")

        result = list_specs(data_root_for(tmp_path))
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Read spec
# ---------------------------------------------------------------------------


class TestReadSpec:
    def test_read(self, monkeypatch, tmp_path):
        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(data_root_for(tmp_path), spec_file)

        result = read_spec(data_root_for(tmp_path), entry["spec_id"])
        assert result["spec_id"] == entry["spec_id"]
        assert result["meta"]["name"] == "Test Device"
        assert "# Test Device Protocol" in result["body"]
        assert result["content"] == VALID_SPEC

    def test_unknown_id(self, monkeypatch, tmp_path):
        with pytest.raises(KeyError):
            read_spec(data_root_for(tmp_path), "nonexistent0000")

    def test_rejects_path_outside_project_in_index(self, monkeypatch, tmp_path):
        """If index.json is tampered to point outside the project, read_spec refuses."""
        import json

        spec_root = data_root_for(tmp_path)
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
            read_spec(data_root_for(tmp_path), "bad")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearchSpec:
    def test_basic(self, monkeypatch, tmp_path):
        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(data_root_for(tmp_path), spec_file)

        results = search_spec(data_root_for(tmp_path), entry["spec_id"], "sensor")
        assert len(results) > 0
        assert results[0]["line"] > 0
        assert "sensor" in results[0]["text"].lower()

    def test_line_numbers(self, monkeypatch, tmp_path):
        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(data_root_for(tmp_path), spec_file)

        results = search_spec(data_root_for(tmp_path), entry["spec_id"], "Commands")
        assert len(results) > 0
        for r in results:
            assert isinstance(r["line"], int)
            assert r["line"] > 0

    def test_k_limit(self, monkeypatch, tmp_path):
        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(data_root_for(tmp_path), spec_file)

        results = search_spec(data_root_for(tmp_path), entry["spec_id"], "a", k=2)
        assert len(results) <= 2

    def test_context(self, monkeypatch, tmp_path):
        spec_file = write_spec(tmp_path, VALID_SPEC)
        entry = register_spec(data_root_for(tmp_path), spec_file)

        results = search_spec(data_root_for(tmp_path), entry["spec_id"], "sensor")
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
