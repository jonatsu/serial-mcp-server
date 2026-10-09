# Contributing

## Dev setup

```bash
# Clone and install in editable mode with test dependencies
git clone https://github.com/es617/serial-mcp-server.git
cd serial-mcp-server
pip install -e ".[test]"

# Run tests (no serial hardware needed)
python -m pytest tests/ -v
```

## Package layout

```text
serial_mcp_server/
├── config.py            # Settings: every SERIAL_MCP_* variable, read once at start
├── confined_files.py    # read a file only inside an operator-chosen directory
├── private_files.py     # create owner-only files with collision-free names
├── plugin_api.py        # the only module plugins import
├── cli.py               # the serial_mcp_script command; imports nothing from mcp/
├── port/                # one open port: connection, stack, buffer, reader, mirror, pacing, writer
├── portlog/             # the port log: log, request, formats
├── script/              # the script engine: parser, matcher, runner, results, connection adapter
├── specs/               # protocol specs: front matter and the spec store
└── mcp/                 # the MCP server: server, state, responses, writes, threads, plugins, trace, tools/
```

Dependencies point one way: `plugin_api` above `mcp/` and the command-line runner, then `script/` and `specs/`, then `port/`, then `portlog/`, then the top-level foundation modules. `just check` and CI run `lint-imports`, which fails on an import against that order; the contracts are in `pyproject.toml`. Tests mirror the package: the tests of `port/writer.py` are in `tests/port/test_writer.py`. The [package layout design](docs/plans/package-layout-design.md) explains the structure.

## How tools are registered

Each module in `serial_mcp_server/mcp/tools/` exports:

```python
TOOLS: list[Tool] = [...]          # Tool definitions with names, descriptions, schemas
HANDLERS: dict[str, Callable] = {  # Maps tool name → async handler function
    "serial.tool_name": handle_fn,
}
```

In `mcp/server.py`, these are merged inside `build_server()`:

```python
tools = connection_tools.TOOLS + io_tools.TOOLS + lines_tools.TOOLS + introspection_tools.TOOLS + ...
handlers = {**connection_tools.HANDLERS, **io_tools.HANDLERS, **lines_tools.HANDLERS, ...}
```

The concatenation order is the order `tools/list` reports, so a new module goes where its tools should appear.

Plugin handlers are added via `mcp/tools/plugin.make_handlers()`, which returns closures that capture the `PluginManager` and `Server` instances.

## Handler pattern

Every handler has the same signature:

```python
async def handle_something(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
```

- `state` — shared serial state (connections), and `state.settings`, the server's configuration
- `args` — parsed tool arguments from the MCP client
- Returns `_ok(key=value)` on success or `_err(code, message)` on failure, from `serial_mcp_server.mcp.responses`

## Blocking calls and cancellation

Handlers use AnyIO, the library the MCP SDK runs on, not `asyncio` directly. Run every blocking call, such as a port operation, a file read, or a thread join, with `anyio.to_thread.run_sync`, and choose what a cancelled request does with it:

- Pass `abandon_on_cancel=True` when the call only produces a value, such as a read, a wait, or a listing. A cancelled request stops waiting, and the thread finishes on its own. Bound such a call with a timeout, because the process waits for its thread at exit.
- Leave the default when the thread builds something that holds a resource, such as opening a port or starting a log. The request waits for the thread, then releases what it built; the cancellation arrives at the next checkpoint, which `anyio.lowlevel.checkpoint()` makes explicit.
- Use `run_to_completion` from `mcp/threads.py` for a call that finishes a change already begun, such as closing a port, stopping a connection, or discarding a log. It runs in a shielded cancel scope, so a pending cancellation cannot skip it.

The SDK cancels a request through the request's cancel scope, and a shielded scope holds against that. A task cancelled directly with `Task.cancel()` goes through shields, so tests cancel a tool call with `tests.helpers.start_request`, which runs the handler in its own cancel scope the way the SDK does. Async tests run on AnyIO's pytest plugin, on the asyncio backend.

## Configuration

`serial_mcp_server/config.py` is the only module that reads the environment. `config.load()` reads every `SERIAL_MCP_*` variable once at start into a frozen `Settings`, and an invalid value stops the start with a message that names every invalid variable. Handlers read `state.settings`; other code takes the values it needs as parameters. To add a variable, add a field to `Settings` with its default and validation, document it in the README's variable table, and pass the value to the code that uses it. A test fails if any other module reads `os.environ`. In tests, build settings with `config.load({...})`, or change one value of a test's state with `tests.helpers.reconfigure`, instead of setting environment variables.

The dispatcher in `mcp/server.py` catches common exceptions (KeyError, SerialException, TimeoutError, etc.) and converts them to error responses automatically.

## Adding a new tool

1. Add the `Tool(...)` definition to the appropriate `mcp/tools/*.py` `TOOLS` list
2. Write the handler function following the signature above
3. Add the mapping to the `HANDLERS` dict
4. Add tests in the corresponding `test_*.py`
5. Keep the text the MCP client sees in step with the behavior: the tool `description` and each property `description` in the `Tool(...)` definition. Change them in the same commit as any change to what a tool does, accepts, returns, or fails with. Write them as instructions for the agent: lead with the trigger ("Use it when the user asks to ..."), state each fact once, phrase the target behavior positively, and share a schema between tools rather than copying it.

Tool names follow the convention `serial.<action>` for core tools (e.g., `serial.read`, `serial.open`) and `serial.<category>.<action>` for subsystems (e.g., `serial.spec.read`, `serial.plugin.reload`).

## Plugin system internals

**Path containment:** `PluginManager.load()` resolves the path and verifies it is inside `plugins_dir` before loading. Paths outside `.serial_mcp/plugins/` are rejected with `ValueError`.

**Loading:** `load_plugin()` uses `importlib` to load a `.py` file or package `__init__.py`. It validates `TOOLS`, `HANDLERS`, and optional `META` exports, and registers the module in `sys.modules` with a unique key (`serial_mcp_plugin__{name}__{hash}`).

**Name collisions:** If a plugin tool name collides with any existing tool (core or other plugin), loading fails with `ValueError`.

**Plugin imports:** plugins import only from `serial_mcp_server.plugin_api`, which re-exports `ok`, `err`, `SerialState`, `Tool`, and the serial read and write handlers. Its `__all__` changes only with a CHANGELOG entry; the template that `serial.plugin.template` returns and the demo plugin follow it.

**Policy:** `SERIAL_MCP_PLUGINS` is parsed into `Settings.plugins`, a `PluginPolicy` of `enabled` and `allowlist`. The `PluginManager` checks this before every `load()` call. `load_all()` skips entirely when disabled.

**Hot reload:** `reload(name)` calls `unload(name)` then `load(path)`. Unload filters the TOOLS list in-place and pops handler keys. The old module is deleted from `sys.modules`.

**Limitation:** MCP clients may not refresh their tool list mid-session. Newly loaded plugins may require a client restart to call their tools. Hot-reload of existing plugins works without restart.

## MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) lets you call tools without an agent:

```bash
npx @modelcontextprotocol/inspector python -m serial_mcp_server
```

Open the URL with the auth token printed in the terminal. Use the **Tools** tab to call any tool interactively.

## Tests

All tests run without serial hardware. They use `MagicMock` for pyserial objects, `tmp_path` fixtures for filesystem isolation, and `monkeypatch` for environment variables.

```bash
# Run all tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/mcp/test_plugins.py -v

# Run a specific test
python -m pytest tests/mcp/test_plugins.py::TestPluginManager::test_load_adds_tools_and_handlers -v
```

### Integration harness

`tests/integration/` holds a slow end-to-end harness. It drives real processes: the demo device (`examples/demo-device/pty_server.py`), the MCP server over stdio, the `serial_mcp_script` CLI, and real pseudo-terminals. It checks that:

- a 4 KiB paste into a mirror terminal at 8 ms per character does not delay device output in `serial.read`, also with a burst paste while the writer is busy with a paced write;
- the jsonl, text, lines, and raw port logs agree on the received bytes and line boundaries;
- `serial.send_file` with chunk pacing shows up as paced tx segments between start and end events;
- a script passes through `serial.script.run`, and the same script passes through `serial_mcp_script` with result files of the same structure;
- a script with a wrong expected answer fails at that line, and the port log holds the device's output;
- a server started without the logging variables creates no log files outside script runs;
- a script with a catastrophic regular expression ends the run `errored` at the pattern time limit, through the server and through `serial_mcp_script`, while the server keeps answering.

Run it from the repository root:

```bash
just integration
```

It takes about 90 seconds and exits 0 only when every scenario passes. It needs Linux, because it reads `/proc` to find its processes, and it uses pseudo-terminals. It starts real processes, and it removes its temporary files and processes when it ends (`--keep-work` keeps the files). It is not part of `pytest` or CI.
