# Contributing

## Dev setup

```bash
# Clone and install the project, its test extra, and the development tools from uv.lock
git clone https://github.com/jonatsu/serial-mcp-server.git
cd serial-mcp-server
uv sync --extra test

# Run tests (no serial hardware needed)
uv run --extra test python -m pytest tests/
```

`uv.lock` is committed, and the development tools (Ruff, basedpyright, import-linter, pre-commit, pip-audit, twine) are\
the `dev` dependency group in `pyproject.toml`, so `just`, CI, and every contributor run the same versions. After\
changing a dependency, run `uv lock` and commit the lock with it; CI installs with `--locked` and fails on a stale\
lock. Ruff is pinned twice, in the `dev` group and as the ruff-pre-commit `rev`; change both together.

## Documentation

Each kind of text has one home, so a reader knows where to look and a fact is written once:

| File | Holds |
| -- | -- |
| `README.md` | What the server is, installation, client setup, and a map of the documents. |
| `docs/concepts.md` | How the parts work and fit together, for a user deciding how to use them. |
| `docs/configuration.md` | Every `SERIAL_MCP_*` variable and the data directory. |
| `docs/tools.md`, `docs/scripts.md` | Reference: each tool's arguments and results, and the script language. |
| `docs/plugins.md` | Plugins, for the people who enable and write them. |
| `docs/roadmap.md` | Known work that no plan covers yet. |
| `CHANGELOG.md` | User-visible changes per release. |
| `CONTRIBUTING.md` | Development: setup, code layout, conventions, tests, and releases. |
| `AGENTS.md` (`CLAUDE.md` imports it) | The few rules a coding agent must not miss, pointing here for the rest. |
| `docs/plans/` | Designs and implementation plans; finished ones move to `docs/plans/archive/`. |

Keep user documents short: explain enough to understand and use a feature, and link to the reference rather than repeat\
it. A behavior change updates the document that owns the behavior in the same commit, alongside the tool text (see\
[Adding a new tool](#adding-a-new-tool)).

## Pre-commit hooks

```bash
pre-commit install                    # run the hooks on every commit
pre-commit run --files <changed files>
```

The hooks check generic hygiene (whitespace, final newlines, merge markers, large files, private keys), scan the staged\
diff for secrets, and check EditorConfig compliance. Ruff formats Python, and mdformat formats Markdown.\
markdownlint-cli2 only reports. Each tool reads its own configuration file (`pyproject.toml`, `.mdformat.toml`,\
`.markdownlint-cli2.jsonc`, `.editorconfig`), so editors with the matching extensions apply the same rules.

In Markdown, keep prose lines at most 120 characters; code blocks, tables, and frontmatter are exempt. mdformat keeps\
line breaks as written, so wrap long lines yourself. Write a hard line break as a trailing backslash (`\`), not two\
trailing spaces.

## Checks and type checking

`just check` runs Ruff, the formatting check, the import-layer contracts, and basedpyright, the type checker. Run it\
with the tests before every commit.

CI (`.github/workflows/cicd.yml`) runs every pre-commit hook on all files, the import-layer contracts, basedpyright,\
the tests on each supported Python version, the tests again against the lowest dependency versions `pyproject.toml`\
allows, the integration harness, the package build, and pip-audit on the locked runtime dependencies. A release tag\
needs all of them to pass, including pip-audit, which elsewhere only reports. CodeQL (`.github/workflows/codeql.yml`)\
analyzes the Python code and the workflows for security flaws; its findings appear in the repository's code scanning\
alerts. Dependabot keeps the actions, which are pinned to commit SHAs, and `uv.lock` current.

basedpyright runs in `standard` mode, configured in `[tool.basedpyright]` in `pyproject.toml`, which the basedpyright\
editor extension reads as well. It checks types and control flow, such as a name that may be unbound or a value that\
may be `None`, without asking for annotations everywhere. Treat a report as a question about the code: fix it when the\
code is wrong, and when the checker misreads correct code, adjust the configuration or add one\
`# pyright: ignore[<rule>]` with a comment that says why. Do not rewrite working code only to satisfy the checker.

In `tests/`, three rules warn instead of failing, for patterns the checker cannot follow there: reading fields off the\
MCP SDK's result unions and off Optional values without narrowing them first, and names assigned inside\
`async with anyio.create_task_group()`, which AnyIO's types make look possibly unbound. Tighten them as the tests gain\
narrowing. The checker assumes Linux, the platform CI runs on.

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

Dependencies point one way: `plugin_api` above `mcp/` and the command-line runner, then `script/` and `specs/`, then\
`port/`, then `portlog/`, then the top-level foundation modules. `just check` and CI run `lint-imports`, which fails on\
an import against that order; the contracts are in `pyproject.toml`. Tests mirror the package: the tests of\
`port/writer.py` are in `tests/port/test_writer.py`. The\
[package layout design](docs/plans/archive/package-layout-design.md) explains the structure.

## Roadmap

[docs/roadmap.md](docs/roadmap.md) lists the known work no plan covers yet: deferred features, accepted limits, and\
test gaps. Record a new follow-up there, not in a plan's execution record, and remove an item when a plan takes it up\
or the change lands.

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

Plugin handlers are added via `mcp/tools/plugin.make_handlers()`, which returns closures that capture the\
`PluginManager` and `Server` instances.

## Handler pattern

Every handler has the same signature:

```python
async def handle_something(state: SerialState, args: dict[str, Any]) -> dict[str, Any]:
```

- `state` — shared serial state (connections), and `state.settings`, the server's configuration
- `args` — parsed tool arguments from the MCP client
- Returns `_ok(key=value)` on success or `_err(code, message)` on failure, from `serial_mcp_server.mcp.responses`

## Blocking calls and cancellation

Handlers use AnyIO, the library the MCP SDK runs on, not `asyncio` directly. Run every blocking call, such as a port\
operation, a file read, or a thread join, with `anyio.to_thread.run_sync`, and choose what a cancelled request does\
with it:

- Pass `abandon_on_cancel=True` when the call only produces a value, such as a read, a wait, or a listing. A cancelled\
  request stops waiting, and the thread finishes on its own. Bound such a call with a timeout, because the process\
  waits for its thread at exit.
- Leave the default when the thread builds something that holds a resource, such as opening a port or starting a log.\
  The request waits for the thread, then releases what it built; the cancellation arrives at the next checkpoint, which\
  `anyio.lowlevel.checkpoint()` makes explicit.
- Use `run_to_completion` from `mcp/threads.py` for a call that finishes a change already begun, such as closing a\
  port, stopping a connection, or discarding a log. It runs in a shielded cancel scope, so a pending cancellation\
  cannot skip it.

The SDK cancels a request through the request's cancel scope, and a shielded scope holds against that. A task cancelled\
directly with `Task.cancel()` goes through shields, so tests cancel a tool call with `tests.helpers.start_request`,\
which runs the handler in its own cancel scope the way the SDK does. Async tests run on AnyIO's pytest plugin, on the\
asyncio backend.

## Configuration

`serial_mcp_server/config.py` is the only module that reads the environment. `config.load()` reads every `SERIAL_MCP_*`\
variable once at start into a frozen `Settings`, and an invalid value stops the start with a message that names every\
invalid variable. Handlers read `state.settings`; other code takes the values it needs as parameters. To add a\
variable, add a field to `Settings` with its default and validation, document it in the variable table of\
`docs/configuration.md`, and pass the value to the code that uses it. A test fails if any other module reads\
`os.environ`. In tests, build settings with `config.load({...})`, or change one value of a test's state with\
`tests.helpers.reconfigure`, instead of setting environment variables.

The dispatcher in `mcp/server.py` catches common exceptions (KeyError, SerialException, TimeoutError, etc.) and\
converts them to error responses automatically.

## Adding a new tool

1. Add the `Tool(...)` definition to the appropriate `mcp/tools/*.py` `TOOLS` list
2. Write the handler function following the signature above
3. Add the mapping to the `HANDLERS` dict
4. Add tests in the corresponding `test_*.py`
5. Keep the text the MCP client sees in step with the behavior: the tool `description` and each property `description`\
   in the `Tool(...)` definition. Change them in the same commit as any change to what a tool does, accepts, returns,\
   or fails with. Write them as instructions for the agent: lead with the trigger ("Use it when the user asks to ..."),\
   state each fact once, phrase the target behavior positively, and share a schema between tools rather than copying\
   it.

Tool names follow the convention `serial.<action>` for core tools (e.g., `serial.read`, `serial.open`) and\
`serial.<category>.<action>` for subsystems (e.g., `serial.spec.read`, `serial.plugin.reload`).

## Plugin system internals

**Path containment:** `PluginManager.load()` resolves the path and verifies it is inside `plugins_dir` before loading.\
Paths outside `.serial_mcp/plugins/` are rejected with `ValueError`.

**Loading:** `load_plugin()` uses `importlib` to load a `.py` file or package `__init__.py`. It validates `TOOLS`,\
`HANDLERS`, and optional `META` exports, and registers the module in `sys.modules` with a unique key\
(`serial_mcp_plugin__{name}__{hash}`).

**Name collisions:** If a plugin tool name collides with any existing tool (core or other plugin), loading fails with\
`ValueError`.

**Plugin imports:** plugins import only from `serial_mcp_server.plugin_api`, which re-exports `ok`, `err`,\
`SerialState`, `Tool`, `ToolAnnotations`, and the serial read and write handlers. Its `__all__` changes only with a\
CHANGELOG entry; the template that `serial.plugin.template` returns and the demo plugin follow it.

**Policy:** `SERIAL_MCP_PLUGINS` is parsed into `Settings.plugins`, a `PluginPolicy` of `enabled` and `allowlist`. The\
`PluginManager` checks this before every `load()` call. `load_all()` skips entirely when disabled.

**Hot reload:** `reload(name)` calls `unload(name)` then `load(path)`. Unload filters the TOOLS list in-place and pops\
handler keys. The old module is deleted from `sys.modules`.

**Limitation:** MCP clients may not refresh their tool list mid-session. Newly loaded plugins may require a client\
restart to call their tools. Hot-reload of existing plugins works without restart.

## MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) lets you call tools without an agent:

```bash
npx @modelcontextprotocol/inspector python -m serial_mcp_server
```

Open the URL with the auth token printed in the terminal. Use the **Tools** tab to call any tool interactively.

## Tests

All tests run without serial hardware. They use `MagicMock` for pyserial objects, `tmp_path` fixtures for filesystem\
isolation, and `monkeypatch` for environment variables.

```bash
# Run all tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/mcp/test_plugins.py -v

# Run a specific test
python -m pytest tests/mcp/test_plugins.py::TestPluginManager::test_load_adds_tools_and_handlers -v
```

### Integration harness

`tests/integration/` holds a slow end-to-end harness. It drives real processes: the demo device\
(`examples/demo-device/pty_server.py`), the MCP server over stdio, the `serial_mcp_script` CLI, and real\
pseudo-terminals. It checks that:

- a 4 KiB paste into a mirror terminal at 8 ms per character does not delay device output in `serial.read`, also with a\
  burst paste while the writer is busy with a paced write;
- the jsonl, text, lines, and raw port logs agree on the received bytes and line boundaries;
- `serial.send_file` with chunk pacing shows up as paced tx segments between start and end events;
- a script passes through `serial.script.run`, and the same script passes through `serial_mcp_script` with result files\
  of the same structure;
- a script with a wrong expected answer fails at that line, and the port log holds the device's output;
- a server started without the logging variables creates no log files outside script runs;
- a script with a catastrophic regular expression ends the run `errored` at the pattern time limit, through the server\
  and through `serial_mcp_script`, while the server keeps answering.

Run it from the repository root:

```bash
just integration
```

It takes about 90 seconds and exits 0 only when every scenario passes. It needs Linux, because it reads `/proc` to find\
its processes, and it uses pseudo-terminals. It starts real processes, and it removes its temporary files and processes\
when it ends (`--keep-work` keeps the files). It is not part of `pytest`; CI runs it on every push.

## Versions and releases

Versions follow [Semantic Versioning](https://semver.org/). The public interface that a version number speaks for is\
the MCP tools (names, arguments, results, and error codes), the `SERIAL_MCP_*` variables,\
`serial_mcp_server.plugin_api`, the script language and its result files, and the exit statuses of `serial_mcp_script`.\
While the version is below 1.0, a release that breaks any of these raises the minor number (0.1.3 to 0.2.0), and a\
release of fixes alone raises the patch number (0.2.0 to 0.2.1). From 1.0 on, a break raises the major number.

The version comes from the Git tag: hatch-vcs derives it when the package is built, so no file holds a version number\
to edit. Releases are published as GitHub releases on this repository, with the wheel and source archive attached.\
Nothing is published to PyPI or the MCP registry from here, because the `serial-mcp-server` package on PyPI and the\
`io.github.es617/serial-mcp-server` registry entry belong to the upstream project.

To release, on an up-to-date `main` with CI green:

1. Rename the CHANGELOG's `## Unreleased` heading to `## X.Y.Z`, add a new empty `## Unreleased` above it, and commit.
2. Optionally tag a release candidate, `vX.Y.Zrc1`, and push the tag. CI builds it and creates a pre-release with\
   generated notes.
3. Tag `vX.Y.Z` on the commit on `main` that holds the final CHANGELOG section, and push the tag. CI runs the checks,\
   builds the package, and creates the release with the CHANGELOG section as its notes. It fails when the CHANGELOG has\
   no `## X.Y.Z` section.
