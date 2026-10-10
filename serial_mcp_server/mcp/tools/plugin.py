"""Plugin management tool definitions and handler factory.

No module-level globals for manager/server — ``make_handlers`` returns
closures that capture them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.plugins import PluginManager
from serial_mcp_server.mcp.responses import _err, _ok
from serial_mcp_server.mcp.state import SerialState


def _plugin_template(device_name: str | None = None) -> str:
    name = device_name or "my_device"
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f'''"""Plugin for {name}."""

# Import only from serial_mcp_server.plugin_api: it is the stable surface for plugins.
# Use the serial handlers instead of conn.ser directly — a reader thread owns the
# serial port for reads, and a writer thread queues and paces every write.
from serial_mcp_server.plugin_api import (
    SerialState,
    Tool,
    err,  # err("code", "message")
    handle_read,  # read raw bytes
    handle_read_until,  # read until a delimiter string
    handle_readline,  # read one line (up to newline)
    handle_write,  # send data to the device
    ok,  # ok(key=val)
)

# Optional metadata — helps the agent match this plugin to a device.
# All fields are optional. Use what makes sense for your device.
META = {{
    "description": "{name} plugin",
    # "device_name_contains": "{name}",
}}

TOOLS = [
    Tool(
        name="{slug}.example",
        description="Example tool — replace with real functionality.",
        inputSchema={{
            "type": "object",
            "properties": {{
                "connection_id": {{"type": "string"}},
            }},
            "required": ["connection_id"],
        }},
    ),
]


async def handle_example(state: SerialState, args: dict) -> dict:
    connection_id = args["connection_id"]
    # Send a command using handle_write + handle_read_until:
    #   await handle_write(state, {{
    #       "connection_id": connection_id,
    #       "data": "COMMAND",
    #       "append_newline": True,
    #   }})
    #   resp = await handle_read_until(state, {{
    #       "connection_id": connection_id,
    #       "delimiter": "> ",
    #       "timeout_ms": 2000,
    #   }})
    #   text = resp["data"]
    #
    # Return errors with: return err("error_code", "Human-readable message")
    # Return success with: return ok(key1=val1, key2=val2)
    return ok(message="Hello from {slug} plugin!")


HANDLERS = {{
    "{slug}.example": handle_example,
}}
'''


def _suggest_plugin_path(plugins_dir: Path, device_name: str | None = None) -> Path:
    name = device_name or "my_device"
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return plugins_dir / f"{slug}.py"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="serial.plugin.list",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use right after serial.open, and whenever a task matches a known device: lists the loaded plugins, "
            "which add device-specific tools such as reading a sensor or flashing firmware, with their tool "
            "names and meta, whose hints such as device_name_contains show which device each fits. Prefer a "
            "plugin's tools to raw reads and writes for the operations it covers. The response also says "
            "whether plugins are enabled. They are off unless the server runs with SERIAL_MCP_PLUGINS set to "
            "all or to a list of plugin names; when off, tell the user to set it where the server is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="serial.plugin.reload",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use after editing a loaded plugin's file: re-imports the plugin and replaces its tools, without "
            "restarting the server. Needs plugins enabled with SERIAL_MCP_PLUGINS."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the loaded plugin to reload.",
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="serial.plugin.template",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use when the user wants shortcut tools for a device that has no plugin, typically after its protocol "
            "is known from a spec: returns a Python plugin template and a suggested_path. Save it at that path, "
            "fill in the tools and handlers, and load it with serial.plugin.load. A plugin runs as code inside "
            "the server, so show it to the user before loading."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "device_name": {
                    "type": "string",
                    "description": "Device name to pre-fill in the template.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="serial.plugin.load",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        description=(
            "Use after saving a new plugin: loads it and adds its tools. The path must be inside the plugins "
            "directory, the plugins_dir that serial.plugin.list reports, and plugins must be enabled with SERIAL_MCP_PLUGINS. The client "
            "may need a restart before it can call the new tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "A .py file, or a directory with __init__.py, inside the plugins directory.",
                },
            },
            "required": ["path"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def make_handlers(manager: PluginManager, server: Server) -> dict[str, Any]:
    """Return handler closures that capture *manager* and *server*."""

    async def handle_plugin_template(_state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
        device_name = args.get("device_name")
        template = _plugin_template(device_name)
        suggested_path = _suggest_plugin_path(manager.plugins_dir, device_name)
        return _ok(template=template, suggested_path=str(suggested_path))

    async def handle_plugin_list(_state: SerialState, _args: dict[str, Any]) -> dict[str, Any]:
        plugins = [
            {"name": info.name, "path": str(info.path), "tools": info.tool_names, "meta": info.meta}
            for info in manager.loaded.values()
        ]
        return _ok(
            plugins=plugins,
            count=len(plugins),
            plugins_dir=str(manager.plugins_dir),
            enabled=manager.enabled,
            policy=manager.policy,
        )

    async def handle_plugin_reload(_state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("name", "")
        if not name:
            return _err("invalid_params", "name is required")
        try:
            info = manager.reload(name)
        except KeyError as exc:
            return _err("not_found", str(exc))
        except PermissionError as exc:
            return _err("plugins_disabled", str(exc))
        except ValueError as exc:
            return _err("plugin_error", str(exc))

        notified = False
        try:
            await server.request_context.session.send_tool_list_changed()
            notified = True
        except Exception:
            pass

        return _ok(
            name=info.name,
            tools=info.tool_names,
            notified=notified,
        )

    async def handle_plugin_load(_state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
        raw_path = args.get("path", "")
        if not raw_path:
            return _err("invalid_params", "path is required")
        try:
            info = manager.load(Path(raw_path))
        except PermissionError as exc:
            return _err("plugins_disabled", str(exc))
        except ValueError as exc:
            return _err("plugin_error", str(exc))

        notified = False
        try:
            await server.request_context.session.send_tool_list_changed()
            notified = True
        except Exception:
            pass

        return _ok(
            name=info.name,
            tools=info.tool_names,
            notified=notified,
            hint="Plugin loaded on the server. The client may need a restart to call the new tools.",
        )

    return {
        "serial.plugin.template": handle_plugin_template,
        "serial.plugin.list": handle_plugin_list,
        "serial.plugin.reload": handle_plugin_reload,
        "serial.plugin.load": handle_plugin_load,
    }
