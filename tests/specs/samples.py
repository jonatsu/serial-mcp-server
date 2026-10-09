"""Spec files and helpers shared by the spec store and spec tool tests."""

from __future__ import annotations

from pathlib import Path

from serial_mcp_server import config
from serial_mcp_server.config import Settings

VALID_SPEC = """\
---
kind: serial-protocol
name: "Test Device"
---

# Test Device Protocol

## Overview

A test device spec.

## Commands

### Read Sensor

- **Write to**: `1234`
- **Format**: `[0x01]`
"""

MINIMAL_SPEC = """\
---
kind: serial-protocol
name: "Minimal"
---

# Minimal Spec
"""


def write_spec(tmp_path: Path, content: str, name: str = "test-device.md") -> Path:
    """Write a spec file and return its path."""
    spec_dir = tmp_path / ".serial_mcp" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_file = spec_dir / name
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


def data_root_for(tmp_path: Path) -> Path:
    """The data root these tests use: tmp_path/.serial_mcp."""
    return tmp_path / ".serial_mcp"


def settings_for(tmp_path: Path) -> Settings:
    return config.load({"SERIAL_MCP_SPEC_ROOT": str(data_root_for(tmp_path))})
