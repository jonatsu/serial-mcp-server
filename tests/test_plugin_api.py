"""Tests for serial_mcp_server.plugin_api — the stable import surface for plugins."""

from __future__ import annotations

from pathlib import Path

import mcp.types

from serial_mcp_server import plugin_api
from serial_mcp_server.mcp.plugins import load_plugin
from serial_mcp_server.mcp.tools.plugin import _plugin_template

PUBLIC_NAMES = [
    "SerialState",
    "Tool",
    "ToolAnnotations",
    "err",
    "handle_read",
    "handle_read_until",
    "handle_readline",
    "handle_write",
    "ok",
]


def test_plugin_api_exports_exactly_the_documented_names():
    assert sorted(plugin_api.__all__) == PUBLIC_NAMES
    assert all(hasattr(plugin_api, name) for name in PUBLIC_NAMES)


def test_ok_and_err_build_tool_responses():
    assert plugin_api.ok(value=1) == {"ok": True, "value": 1}
    assert plugin_api.err("bad", "Nope.") == {"ok": False, "error": {"code": "bad", "message": "Nope."}}


async def test_a_plugin_written_from_the_template_loads_and_its_tool_runs(tmp_path: Path, serial_state):
    plugin_file = tmp_path / "probe_device.py"
    plugin_file.write_text(_plugin_template("Probe Device"), encoding="utf-8")

    name, tools, handlers, _module_key, _meta = load_plugin(plugin_file)
    result = await handlers["probe_device.example"](serial_state, {"connection_id": "s1"})

    assert name == "probe_device"
    assert [tool.name for tool in tools] == ["probe_device.example"]
    assert result == {"ok": True, "message": "Hello from probe_device plugin!"}


def test_the_template_imports_only_from_plugin_api():
    imports = [line for line in _plugin_template("x").splitlines() if line.startswith(("from ", "import "))]

    assert imports == ["from serial_mcp_server.plugin_api import ("]


def test_our_mcp_package_never_shadows_the_sdk():
    import serial_mcp_server.mcp.server  # loads our package beside the SDK

    package_dir = Path(serial_mcp_server.__file__).parent.resolve()

    assert package_dir not in Path(mcp.types.__file__).resolve().parents
