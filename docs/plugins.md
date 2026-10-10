# Plugins

A plugin is a Python module that adds device-specific tools to the server. The built-in tools speak bytes: open a port,\
write, read. A plugin turns a device's protocol into named operations, such as `demo.status` or `gps.get_position`, so\
the agent calls one tool instead of composing writes and reads, and gets parsed results back. The agent sees plugin\
tools exactly like the built-in ones, in the same tool list.

Plugins are optional. Without them the agent still reaches every device through the built-in tools, and a\
[protocol spec](concepts.md#protocol-specs--teaching-the-agent-about-your-device) is often enough to teach it a\
protocol. Write a plugin when the same operations come up again and again, when a reply needs parsing, or when a\
multi-step exchange should always run the same way.

## Enabling plugins

Plugins are off by default. `SERIAL_MCP_PLUGINS` turns them on when the server starts:

| Value | Effect |
| -- | -- |
| unset | Plugins are disabled; none load. |
| `all` | Every plugin in the plugins directory may load. |
| `name1,name2` | Only the named plugins may load. |

Set it where the MCP client starts the server, for example:

```bash
claude mcp add serial -e SERIAL_MCP_PLUGINS=all -- serial_mcp
```

A plugin runs as code inside the server process, with the server's permissions: it can read and write files, open\
network connections, and drive any open port. Enable only plugins you have read or trust, and prefer a name list to\
`all` when the directory holds code you have not reviewed.

## Installing a plugin

Plugins live in the `plugins` directory of the data root, `.serial_mcp/plugins/` by default (`SERIAL_MCP_SPEC_ROOT`\
moves the data root). A plugin is either one file, `.serial_mcp/plugins/<name>.py`, or a package directory,\
`.serial_mcp/plugins/<name>/__init__.py`. The file or directory name is the plugin's name, which `SERIAL_MCP_PLUGINS`\
lists; `all` is reserved.

When the server starts, it loads every allowed plugin in the directory. A plugin that fails to load is logged and\
skipped, and the others still load. While the server runs:

- `serial.plugin.list` shows the loaded plugins, their tools, and whether plugins are enabled.
- `serial.plugin.load` loads a new plugin from the plugins directory. Paths outside it are refused.
- `serial.plugin.reload` re-imports a loaded plugin after its file changes.

The server tells the client when its tool list changes, but some clients read the list only once per session. If newly\
loaded tools do not appear, restart the client; editing an already-loaded plugin and reloading it works without a\
restart.

To try one, copy the demo device's plugin and load it:

```bash
mkdir -p .serial_mcp/plugins
cp examples/demo-device/demo_device_plugin.py .serial_mcp/plugins/
```

Then start the server with `SERIAL_MCP_PLUGINS=all` and ask the agent to open the demo device; see\
[the demo device](../examples/demo-device/README.md).

## Writing a plugin

A plugin module defines three names:

```python
from serial_mcp_server.plugin_api import SerialState, Tool, ToolAnnotations, handle_read_until, handle_write, ok

META = {"description": "DemoDevice plugin", "device_name_contains": "DemoDevice"}  # optional

TOOLS = [
    Tool(
        name="demo.version",
        description="Use to read the DemoDevice firmware version.",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        inputSchema={
            "type": "object",
            "properties": {"connection_id": {"type": "string"}},
            "required": ["connection_id"],
        },
    ),
]


async def version(state: SerialState, args: dict) -> dict:
    await handle_write(state, {"connection_id": args["connection_id"], "data": "version", "append_newline": True})
    reply = await handle_read_until(state, {"connection_id": args["connection_id"], "delimiter": "> "})
    return ok(version=reply["data"].removesuffix("> ").strip())


HANDLERS = {"demo.version": version}
```

`TOOLS` lists the MCP tool definitions, and `HANDLERS` maps each tool name to an async function that takes the server\
state and the call's arguments and returns a result. Every tool needs exactly one handler, and a tool name must not\
collide with a built-in or another plugin's tool; name tools `<plugin>.<action>`. `META` is free-form metadata that\
`serial.plugin.list` shows the agent, so it can match the plugin to a device; `description` and `device_name_contains`\
are the common keys.

Import only from `serial_mcp_server.plugin_api`. It provides `ok` and `err` to build results, `SerialState`, `Tool`,\
`ToolAnnotations`, and the handlers `handle_write`, `handle_read`, `handle_readline`, and `handle_read_until`. That\
module is the plugin interface: its names change only with a CHANGELOG entry, while the server's other modules can move\
between releases. Reach the device through those handlers rather than the port object, because the server's reader\
thread owns reads and its writer thread queues and paces every write, which keeps the port log, the mirror, and a\
running script consistent. A plugin that reads the port object directly gets the server's fixed 200 ms port read\
timeout, not the connection's `timeout_ms`, which sets only the handlers' default wait.

Write tool descriptions for the agent: say when to use the tool, what it returns, and what it changes on the device.\
Add MCP `annotations`, such as `readOnlyHint`, where they apply; the server passes them to the client unchanged. An\
exception a handler raises becomes an error result, so a `KeyError` for an unknown connection returns `not_found`, and\
a `ValueError` returns `invalid_params`.

## Letting the agent write plugins

The agent can draft a plugin itself. After it has learned a device's protocol, from a spec or by exploring,\
`serial.plugin.template` gives it a skeleton and a path to save it at. It fills in the tools, and `serial.plugin.load`\
makes them available. Because the plugin then runs inside the server, read the code before you let the agent load it.
