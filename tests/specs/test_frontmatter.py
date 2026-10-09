"""Tests for serial_mcp_server.specs.frontmatter — parsing, validation, and spec ids."""

from __future__ import annotations

from serial_mcp_server.specs.store import (
    compute_spec_id,
    parse_frontmatter,
    validate_spec_meta,
)

# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------
from tests.specs.samples import VALID_SPEC


class TestParseFrontmatter:
    def test_valid(self):
        meta, body = parse_frontmatter(VALID_SPEC)
        assert meta["kind"] == "serial-protocol"
        assert meta["name"] == "Test Device"
        assert "# Test Device Protocol" in body

    def test_missing(self):
        content = "# No frontmatter here\n\nJust markdown."
        meta, body = parse_frontmatter(content)
        assert meta == {}
        assert body == content

    def test_invalid_yaml(self):
        content = "---\n[invalid yaml:: {\n---\nBody\n"
        meta, body = parse_frontmatter(content)
        assert meta == {}
        assert body == content

    def test_non_dict_yaml(self):
        content = "---\n- just\n- a\n- list\n---\nBody\n"
        meta, body = parse_frontmatter(content)
        assert meta == {}
        assert body == content


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidateSpecMeta:
    def test_valid(self):
        assert validate_spec_meta({"kind": "serial-protocol", "name": "Foo"}) == []

    def test_missing_kind(self):
        errors = validate_spec_meta({"name": "Foo"})
        assert len(errors) == 1
        assert "kind" in errors[0]

    def test_wrong_kind(self):
        errors = validate_spec_meta({"kind": "other", "name": "Foo"})
        assert len(errors) == 1

    def test_missing_name(self):
        errors = validate_spec_meta({"kind": "serial-protocol"})
        assert len(errors) == 1
        assert "name" in errors[0]

    def test_empty_name(self):
        errors = validate_spec_meta({"kind": "serial-protocol", "name": ""})
        assert len(errors) == 1

    def test_multiple_errors(self):
        errors = validate_spec_meta({})
        assert len(errors) == 2


# ---------------------------------------------------------------------------
# Spec ID
# ---------------------------------------------------------------------------


class TestComputeSpecId:
    def test_deterministic(self, tmp_path):
        p = tmp_path / "test.md"
        p.touch()
        id1 = compute_spec_id(p)
        id2 = compute_spec_id(p)
        assert id1 == id2
        assert len(id1) == 16

    def test_different_paths(self, tmp_path):
        p1 = tmp_path / "a.md"
        p2 = tmp_path / "b.md"
        p1.touch()
        p2.touch()
        assert compute_spec_id(p1) != compute_spec_id(p2)
