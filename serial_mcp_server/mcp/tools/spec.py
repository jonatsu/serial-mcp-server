"""Spec tool definitions and handlers — template, register, list, attach, read, search."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool, ToolAnnotations

from serial_mcp_server.mcp.responses import _err, _ok
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.tools.schemas import CONNECTION_ID
from serial_mcp_server.specs import store

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_SPEC_ID = {"type": "string", "description": "The spec_id from serial.spec.list or serial.spec.register."}

TOOLS: list[Tool] = [
    Tool(
        name="serial.spec.template",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use when a device has no protocol spec and the user wants one: returns a Markdown template to fill "
            "in with the commands, responses, and flows you learn, and a suggested_path. Save it there and "
            "register it with serial.spec.register."
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
        name="serial.spec.register",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        description=(
            "Use after writing or receiving a protocol spec file, so later sessions find it with "
            "serial.spec.list: adds the file to the spec index and returns its spec_id. The file must lie inside "
            "the project directory, the one that holds the data directory (.serial_mcp/ by default), and its "
            "YAML front matter needs "
            "kind: serial-protocol and a name. Registering the same file again refreshes its entry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the spec's Markdown file, absolute or relative to the working directory.",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="serial.spec.list",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use right after serial.open, and whenever a device's commands are unknown: lists the registered "
            "protocol specs with their metadata and the hints that match a spec to a device."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="serial.spec.attach",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        description=(
            "Use when serial.spec.list shows a spec that matches the open device. Attaches a registered spec "
            "to a connection for this session only. "
            "The spec will be available via serial.spec.get for the duration of this connection. "
            "After attaching, check serial.plugin.list for a matching plugin, then present "
            "the user with their options: "
            "interact with the device using the spec (send commands, execute flows), "
            "use plugin shortcut tools if a plugin is loaded, "
            "extend an existing plugin with new tools, "
            "or create a new plugin using serial.plugin.template."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
                "spec_id": {
                    "type": "string",
                    "description": "The spec_id from serial.spec.list or serial.spec.register.",
                },
            },
            "required": ["connection_id", "spec_id"],
        },
    ),
    Tool(
        name="serial.spec.get",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use to recall which protocol spec applies to a connection: returns the attached spec, or null "
            "when none is attached."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": CONNECTION_ID,
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="serial.spec.read",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use before sending commands to a device with a spec: returns the spec's full content, file path, "
            "and metadata. For one command or topic, serial.spec.search is shorter."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "spec_id": _SPEC_ID,
            },
            "required": ["spec_id"],
        },
    ),
    Tool(
        name="serial.spec.search",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Use to look up one command, response, or error in a long spec: returns the matching snippets "
            "with line numbers and surrounding context."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "spec_id": _SPEC_ID,
                "query": {"type": "string", "description": "Search terms (space-separated)."},
                "k": {
                    "type": ["integer", "string"],
                    "description": "Max results to return (default 10).",
                    "default": 10,
                },
            },
            "required": ["spec_id", "query"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_spec_template(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    device_name: str | None = args.get("device_name")
    template = store.get_template(device_name)
    suggested_path = store.suggest_spec_path(state.settings.data_root, device_name)
    return _ok(template=template, suggested_path=str(suggested_path))


async def handle_spec_register(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    path = args["path"]
    try:
        entry = store.register_spec(state.settings.data_root, path)
    except FileNotFoundError as exc:
        return _err("not_found", str(exc))
    except ValueError as exc:
        return _err("invalid_spec", str(exc))
    return _ok(**entry)


async def handle_spec_list(state: SerialState, _args: dict[str, Any]) -> dict[str, Any]:
    entries = store.list_specs(state.settings.data_root)
    return _ok(specs=entries, count=len(entries))


async def handle_spec_attach(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    cid = args["connection_id"]
    spec_id = args["spec_id"]
    entry = state.get_connection(cid)
    try:
        spec_data = store.read_spec(state.settings.data_root, spec_id)
    except KeyError as exc:
        return _err("not_found", str(exc))
    except FileNotFoundError as exc:
        return _err("not_found", str(exc))
    entry.spec = {
        "spec_id": spec_data["spec_id"],
        "path": spec_data["path"],
        "meta": spec_data["meta"],
    }
    return _ok(
        spec_id=spec_id,
        path=spec_data["path"],
    )


async def handle_spec_get(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    cid = args["connection_id"]
    entry = state.get_connection(cid)
    if entry.spec is None:
        return _ok(spec=None)
    return _ok(spec=entry.spec)


async def handle_spec_read(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    spec_id = args["spec_id"]
    try:
        data = store.read_spec(state.settings.data_root, spec_id)
    except KeyError as exc:
        return _err("not_found", str(exc))
    except FileNotFoundError as exc:
        return _err("not_found", str(exc))
    return _ok(**data)


async def handle_spec_search(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
    spec_id = args["spec_id"]
    query = args["query"]
    k = int(args.get("k", 10))
    try:
        results = store.search_spec(state.settings.data_root, spec_id, query, k=k)
    except KeyError as exc:
        return _err("not_found", str(exc))
    except FileNotFoundError as exc:
        return _err("not_found", str(exc))
    return _ok(results=results, count=len(results))


HANDLERS: dict[str, Any] = {
    "serial.spec.template": handle_spec_template,
    "serial.spec.register": handle_spec_register,
    "serial.spec.list": handle_spec_list,
    "serial.spec.attach": handle_spec_attach,
    "serial.spec.get": handle_spec_get,
    "serial.spec.read": handle_spec_read,
    "serial.spec.search": handle_spec_search,
}
