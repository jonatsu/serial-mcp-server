# Package Layout and Configuration Implementation Plan

Status: ready for implementation, 9 October 2026, after plan-critic review. No unit has started; the
[Execution Record](#execution-record) holds results as units land.
Basis: `main` at `2a13952`, and the accepted [package layout design](package-layout-design.md) on branch
`refactor/package-layout`. The design owns the target structure, the configuration model, the plugin interface, and
the switch to AnyIO; this plan owns the order and the checks.

## Implementation Overview

The refactor turns a flat package of 25 top-level modules into the capability packages of design section 5.1, puts
every `SERIAL_MCP_*` variable behind one `Settings` model, and ends with the async code on AnyIO. Three kinds of
work happen, and the order follows from them.

Unit 1 changes behavior on purpose: settings are read once at start, and an invalid value stops the start (design
decisions L3 and L4). It runs first, on today's module layout, because every later move then carries a `Settings`
value instead of an environment read, and because a behavior change is easiest to review when no file has moved.

Units 2 to 9 move code. They go bottom-up through the layers of design section 5.2, so a unit only updates imports
in modules that have not moved yet, never in a package that a later unit reshapes again. Each unit moves its tests
with its code into the mirrored `tests/` tree. From unit 3 on, an `import-linter` contract checks the packages that
exist so far, locally and in CI, and each unit extends it; unit 10 completes it to the full layer order. Unit 7 also
creates the plugin interface, because it removes the modules that plugins import today, so the demo plugin and the
template move to `plugin_api` once instead of twice. Unit 11 switches the async code to AnyIO, last, because it
changes cancellation behavior and must not hide inside a move.

```mermaid
flowchart LR
    U1[1 Settings] --> U2[2 file modules]
    U2 --> U3[3 port/]
    U3 --> U4[4 portlog/]
    U4 --> U5[5 script/]
    U1 --> U6[6 specs/]
    U5 --> U7[7 mcp/ and plugin_api]
    U6 --> U7
    U7 --> U8[8 serial tools split]
    U7 --> U9[9 cli.py]
    U8 --> U10[10 full layer contract]
    U9 --> U10
    U10 --> U11[11 AnyIO]
    U11 --> F[Final integration]
```

Units 2 to 6, 8, and 9 change no tool's schema, result, or error code, and no test outcome; their test count
changes only by the checks this plan adds.

## Shared Preflight

### P1: Environment and Baseline

Work on `refactor/package-layout`, one branch for the whole plan; do not create a branch per unit. Record the
baseline once:

```bash
uv run --extra test python -m pytest tests/ -q
just check
uv run --with bandit bandit -r serial_mcp_server/ -c pyproject.toml
just integration
```

`just check` runs Ruff's lint and format checks, and from unit 3 on also `lint-imports`. `just integration` runs the
end-to-end harness in `tests/integration/` against the demo device over real pseudo-terminals; it takes about 90
seconds and needs Linux. The baseline at `2a13952` is 704 passing tests, clean checks, and a passing harness. Record
any other result before editing. `uv.lock` is ignored, so `uv run` leaves the tree clean.

### P2: Guidance

Load before editing the matching work, and show compliance in the review evidence:

- `coding-standards` and `python-style` for every code change.
- `python-architecture` for every move: package boundaries, `__init__.py` jobs, and import direction.
- `python-error-handling` for `config.load` and `ConfigError` (unit 1).
- `python-testing` for moved and new tests, including the `tests/` subpackages.
- `python-async-patterns` for unit 11.
- `writing-documentation` for `README.md`, `CONTRIBUTING.md`, `docs/`, and `CHANGELOG.md`; `writing-for-agents`
  for text an agent reads, which here is the plugin template that `serial.plugin.template` returns (unit 7).

Before moving, renaming, or deleting a symbol, run the language server's `findReferences` on it and confirm the list
with `rg` for the name; the server misses files it has not indexed. Also run `rg '"serial_mcp_server\.'` in each
moving unit: `monkeypatch.setattr` and `patch` targets name modules in strings, which neither the language server nor
the import system checks until the test runs. Move files with `git mv`, so history follows them. When a module
splits, `git mv` it to the piece that keeps most of its code, and create the other pieces. Every new `tests/`
subdirectory gets an empty `__init__.py`, because `tests/` is a package and test modules in different
subdirectories share basenames such as `test_state.py`.

### P3: Documentation and Change Log

A unit that changes behavior, a variable, or an import path that users or plugin authors see updates `README.md`,
`docs/`, and the `## Unreleased` section of `CHANGELOG.md`. Every unit that moves a module updates the module names
in `CONTRIBUTING.md` and `docs/concepts.md`. When the as-built structure differs from the design, update the design
in the same unit.

### P4: Review and Commit Gate

The implementer runs the P1 commands, reviews the diff, and records the results. `just integration` runs in units 1,
5, 7, 9, and 11, whose changes reach the server start, the result files, plugin loading, or the command-line runner;
the other units rely on the unit tests and the final integration run. Units 1, 7, 8, and 11 change behavior,
entry points, or interfaces; each gets an independent `code-reviewer` pass on the exact diff and test evidence
before dependent work starts, with `model: opus` and effort `medium` for unit 11, which changes cancellation. The
other units are mechanical moves and get a documented self-review: rename detection with `git diff -M --stat`, an
unchanged test outcome, `lint-imports`, and a diff with no change beyond imports and moved code. The final
integration review covers them together. The implementer fixes findings, and the reviewer rechecks only the changed
findings and affected evidence. Blocking findings close before the commit.

Commit each unit as one or more Conventional Commits with explicit pathspecs. Pushing and merging need the user's
authorization.

## Unit 1: One Settings Model

### Outcome and Rationale

Every `SERIAL_MCP_*` variable is read once, at process start, into one frozen `Settings`, and an invalid value stops
the start with one message that names every invalid variable (design section 8.1, decisions L3 and L4). Operators
get one table of variables, and `SERIAL_MCP_TRACE=off` turns tracing off.

### Changes

Add `serial_mcp_server/config.py` with the `Settings` model of design section 8.1, built on
`pydantic_settings.BaseSettings` with `frozen=True` and with `settings_customise_sources` returning only the
initializer source, so the model never reads the process environment by itself. Every field has a default, so
`Settings()` is valid; the default of `data_root` walks up from the working directory as `specs.resolve_spec_root`
does today, which moves into `config.py`. `files_dir`, `scripts_dir`, and `log_dir` default to their directory under
`data_root`. Add `ConfigError`, which lists every problem by variable name, and `load(environ: Mapping[str, str] =
os.environ) -> Settings`, which takes the `SERIAL_MCP_*` entries, drops blank values, and builds the model. Booleans
use Pydantic's parsing, choice fields lowercase their input before validation, and list fields split on commas.
Declare `pydantic-settings` as a dependency, because `config.py` imports it directly.

Then remove each environment read and give its consumer the value:

| Reader today                                     | Gets its value from                                                    |
| ------------------------------------------------ | ---------------------------------------------------------------------- |
| `helpers.MAX_CONNECTIONS`, `MIRROR_PTY`, `MIRROR_PTY_LINK`, `max_write_seconds()` | `state.settings`                 |
| `handlers_script.scripts_dir()`, `paths.files_dir()` | `state.settings`; the CLI passes its own `Settings`                |
| `portlog.resolve_log_request`, `build_log_request`, `log_directory`, the timestamp-mode parsing | a `Settings` parameter |
| `script.runner.ScriptRunner._read_max_seconds`   | a `max_seconds` constructor parameter                                   |
| `trace` module constants and `_buffer`           | a `TraceBuffer` built by `init_trace(settings)`, which holds the enabled flag, payload flag, and size limit; `sanitize_args` and `status` read them from it |
| `plugins.parse_plugin_policy`                    | `settings.plugin_policy`                                                |
| `server._TOOL_SEP`, `_LOG_LEVEL`, and the module-level `logging.basicConfig` | `main`, after `config.load()`      |
| `specs.resolve_spec_root` callers                | `settings.data_root`                                                    |

`SerialState` gains `settings: Settings = field(default_factory=Settings)`, so its 37 test constructions and
`tests/conftest.py` keep working without reading the environment, and `max_connections` comes from the settings
instead of a constructor argument. `build_server(settings)` takes the settings and passes them to `SerialState` and
`init_trace`. `init_trace` always replaces the buffer, so a disabled trace leaves no stale buffer behind.
`server.main` calls `config.load()`; on `ConfigError` it writes the message to stderr and exits 2 before the
transport starts (design section 6.1). `script_cli.main` loads settings, applies `--log-dir`, `--files-dir`,
`--log-format`, and `--no-log` with `model_copy(update=...)`, and exits 2 on `ConfigError` (design section 6.2). The
CLI therefore also rejects an invalid server-only variable, such as `SERIAL_MCP_MIRROR`, which decision L4 implies;
the CHANGELOG says so.

Tests that set `SERIAL_MCP_*` variables build a `Settings` or call `config.load({...})` instead, including the
`TRACE_*` and `_buffer` patches in `tests/test_trace.py`, the direct `os.environ` read in
`tests/test_handlers_script.py`, and the `max_connections=` constructions in `tests/test_state.py` and
`tests/test_handlers_serial.py`. The test in `tests/test_handlers_script.py` that asserts a per-call read inverts:
it asserts that an environment change after start has no effect. Add tests for each variable's default, accepted
forms, and rejected forms; a message that names every invalid variable at once; `SERIAL_MCP_TRACE=off`; and the
server and the CLI exiting 2. Add a test that searches the package source for `os.environ` and `getenv` and fails on
any match outside `config.py`.

Document in `README.md` the whole variable table, including `SERIAL_MCP_SPEC_ROOT`, which no user document lists
today, and the startup error. List the behavior changes of design section 8.1, and the CLI's stricter start, under
`### Changed` in `CHANGELOG.md`.

### Dependencies and Constraints

None before it. Keep every module where it is: moving files at the same time would hide the behavior change in
rename noise. Tool error codes for a bad argument stay as they are; only invalid environment values move to start.

### Verification and Completion

The suite passes, with the new configuration tests. The search test passes, and `rg 'os\.environ|getenv'
serial_mcp_server` matches only `config.py`. `SERIAL_MCP_MIRROR=bogus SERIAL_MCP_LOG_LEVEL=loud uv run python -m
serial_mcp_server` exits 2 and names both variables. `just integration` passes, which shows that the server and the
CLI still start with the harness's environment. Independent review per P4.

### Recovery

A test that still needs a per-call environment read shows a consumer that was missed: give it the value from
`Settings`, never a new environment read.

## Unit 2: File Access Modules

### Outcome and Rationale

Confined reads and private file creation each live in one module named for them (design section 5.3).

### Changes

`git mv serial_mcp_server/paths.py serial_mcp_server/confined_files.py`. Create `private_files.py` from
`portlog.open_exclusive`, `log_base_name`, `DIR_MODE`, `FILE_MODE`, and `MAX_NAME_COLLISIONS`. Update the importers,
including `script/runner.py` and `script/directives.py`, the string patch targets, and the module name in the
probe of `tests/test_paths.py`, which moves to `tests/test_confined_files.py`.

### Dependencies and Constraints

Unit 1, so `files_dir` is already a setting.

### Verification and Completion

Same test outcome; `git diff -M --stat` shows `paths.py` as a rename; `rg 'serial_mcp_server\.paths\b|import paths'`
matches nothing. Self-review per P4.

## Unit 3: The `port/` Package

### Outcome and Rationale

Everything about one open port lives in `port/`, one concern per module, and the command-line runner no longer
needs the MCP server's state module for its connection type (design sections 5.1 and 5.3).

### Changes

Create `serial_mcp_server/port/` with an empty `__init__.py`. Move `SerialConnection` and `stop_connections` from
`state.py` to `port/connection.py`. Split `mirror.py`: `SerialBuffer` to `port/buffer.py`; `ReaderThread`,
`_ReaderSubscriber`, and `create_reader` to `port/reader.py`; `MirrorSession`, `_IS_UNIX`, and the mirror index
helpers to `port/mirror.py`. Move `port_stack.py` to `port/stack.py`. Split `writer.py`: `Pacing`, `NO_PACING`, and
`MAX_PACING_DELAY_MS` to `port/pacing.py`, the rest to `port/writer.py`. Move `paced_transfer_seconds` and
`wire_seconds` from `handlers_serial.py` to `port/pacing.py`.

Move the matching tests under `tests/port/`. `tests/test_mirror.py` covers buffer, reader, and mirror together;
split it along the same lines, changing only imports and patch targets such as
`serial_mcp_server.port_stack.create_reader` and `mirror._IS_UNIX`.

Add `import-linter` here: a `lint-imports` step in the CI lint job of `.github/workflows/cicd.yml`, a `just` recipe
that runs it through `uv run --with import-linter`, `check` depending on that recipe, and a forbidden contract in
`pyproject.toml` that keeps `serial_mcp_server.port` from importing `script`, `state`, `server`, `helpers`, and every
`handlers_*` module.

### Dependencies and Constraints

Unit 2. `port/` may import `portlog`, `config`, and the file modules.

### Verification and Completion

Same test outcome; the PTY tests still pass on Linux; `just check` passes with the new contract. Add a temporary
import of `serial_mcp_server.state` to `port/writer.py`, confirm `just check` fails and names the contract, then
remove it; record both runs. Self-review per P4.

## Unit 4: The `portlog/` Package

### Outcome and Rationale

The port log's thread, its request building, and its file formats each have their own module (design section 5.3).

### Changes

Create `portlog/` with `log.py` (`PortLog`, `PortLogSlot`), `request.py` (`LogRequest`, `parse_formats`,
`resolve_log_request`, `build_log_request`, the timestamp-mode parsing, and `log_directory`), and `formats.py`
(`git mv` of `portlog_formats.py`). Move the tests under `tests/portlog/`. Extend the import contract so `portlog`
imports nothing from `port`, `script`, or the MCP modules.

### Dependencies and Constraints

Unit 3, because `port/writer.py` and `port/reader.py` import `PortLogSlot`.

### Verification and Completion

As in unit 3, without the temporary-import run. Self-review per P4.

## Unit 5: The Script Engine's Results and Adapter

### Outcome and Rationale

The runner executes scripts, a separate module owns results and result files, the adapter between a connection and
the engine lives with the engine, and the engine owns its cancel wait (design section 5.3).

### Changes

Move `RunState`, `RunMetadata`, `RunStatus`, `StepResult`, `RunFailure`, `RunResult`, and the result-file building
and writing (today `ScriptRunner._build_result`, `_open_result`, and `_write_result`) to `script/results.py`; the
runner calls them. `git mv connection_session.py script/connection_session.py`. Move `SCRIPT_CANCEL_WAIT_SECONDS`
from `state.py` to `script/runner.py`, next to the cancel it bounds; `script_cli.py`, `handlers_script.py`, and
`handlers_serial.py` import it from there. Move the script tests under `tests/script/`. Extend the import contract so
`script` imports nothing from the MCP modules.

### Dependencies and Constraints

Units 3 and 4.

### Verification and Completion

Same test outcome. `just integration` passes, and a result file from its script scenarios has the same keys as
before. Self-review per P4.

## Unit 6: The `specs/` Package

### Outcome and Rationale

The spec store's parsing is separate from its storage (design section 5.3).

### Changes

Split `specs.py` into `specs/frontmatter.py` (`parse_frontmatter`, `validate_spec_meta`, `compute_spec_id`) and
`specs/store.py` (index, register, list, read, search, template, and path suggestion). Unit 1 already moved the
data-root walk. Move `tests/test_specs.py` under `tests/specs/`. Extend the import contract so `specs` imports
nothing from the MCP modules.

### Dependencies and Constraints

Unit 1. Independent of units 2 to 5.

### Verification and Completion

Same test outcome. Self-review per P4.

## Unit 7: The `mcp/` Package and the Plugin Interface

### Outcome and Rationale

The MCP server, its state, its responses, and its tools live in `mcp/`; `helpers.py` and `state.py` cease to exist;
and plugins import only from `plugin_api` (design sections 5.1, 5.3, and 8.2, decision L5). The plugin interface
lands here because this unit removes the modules that plugins import today.

### Changes

Move `server.py` to `mcp/server.py`; the remainder of `state.py` (`SerialState`, `ScriptJobs`, `ScriptRun`) to
`mcp/state.py`; `trace.py` to `mcp/trace.py`; `plugins.py` to `mcp/plugins.py`. Split `helpers.py`: `_ok`, `_err`,
`_result_text`, `_coerce_bool`, and `script_running_error` go to `mcp/responses.py`, and the module ends. Move
`run_write_job` and `duration_error` from `handlers_serial.py` to `mcp/writes.py`. Move each `handlers_<name>.py` to
`mcp/tools/<name>.py`, with `handlers_serial.py` moved whole as `mcp/tools/serial.py` until unit 8. Point
`__main__.py` and the `serial_mcp` entry point at `serial_mcp_server.mcp.server:main`. Move the tests under
`tests/mcp/`.

Add `plugin_api.py` with `__all__` listing `ok`, `err`, `SerialState`, `Tool`, `handle_write`, `handle_read`,
`handle_readline`, and `handle_read_until`, re-exported from their new homes. Change the template in
`mcp/tools/plugin.py` and the demo plugin in `examples/demo-device/demo_device_plugin.py` to import from
`plugin_api` alone. Update the plugin sections of `docs/tools.md` and `CONTRIBUTING.md`, and add a `### Changed`
CHANGELOG entry, marked breaking, that gives the old and new imports.

Extend the probe in `tests/test_script_cli.py` that checks the command loads no `mcp` module, so it also rejects
`serial_mcp_server.mcp` and its submodules. Add a test that imports `mcp.types` and `serial_mcp_server.mcp.server`
in one process and asserts that `mcp.types.__file__` lies outside `serial_mcp_server/`, so the SDK is never shadowed.
Add tests that a plugin written from the template loads and its tool runs, and that `plugin_api.__all__` names
exactly the listed symbols. Extend the import contract so `plugin_api` and the MCP package sit above `script`,
`specs`, `port`, and `portlog`.

### Dependencies and Constraints

Units 5 and 6. Inside the package, the SDK is imported by absolute name (`mcp.types`, `mcp.server`), which resolves
to the SDK, and our modules as `serial_mcp_server.mcp...`. `anyio` stays an undeclared transitive import until
unit 11 declares it.

### Verification and Completion

The suite passes with the new tests. Compare `tools/list` with the base: create a worktree at `2a13952` with `git
worktree add`, start the server from each tree with the same environment, request `tools/list` from both over
stdio, and diff the JSON, which must match exactly, including order. Then remove the worktree. `just integration`
passes with the demo plugin loaded. Independent review per P4, because this unit changes entry points, tool
registration, and the plugin contract.

### Recovery

If an absolute `import mcp` inside the package resolves to `serial_mcp_server.mcp`, a module is on `sys.path`
directly or uses an implicit relative import; fix that, never rename the SDK import.

## Unit 8: Serial Tools by Concern

### Outcome and Rationale

The 1,084-line serial tool module becomes three modules: connections, input and output, and control lines (design
section 5.1).

### Changes

Split `mcp/tools/serial.py` into `mcp/tools/connection.py` (`list_ports`, `open`, `close`, `configure`,
`connection_status`, with the open-cancellation helper), `mcp/tools/io.py` (`read`, `readline`, `read_until`,
`write`, `flush`, and `parse_write_pacing`, which parses tool arguments), and `mcp/tools/lines.py` (the DTR and RTS
tools). Each keeps its own `TOOLS` and `HANDLERS`, and `mcp/server.py` registers all three in today's tool order.
Repoint the re-exports in `plugin_api.py`; plugins do not change. Split the serial tool tests along the same lines,
updating patch targets such as `handlers_serial.pyserial.Serial`, `open_port_stack`, and `stop_connections`.

### Dependencies and Constraints

Unit 7. `mcp/tools/file.py` imports `parse_write_pacing` from `io.py` and the write helpers from `mcp/writes.py`.

### Verification and Completion

Same test outcome; the `tools/list` comparison of unit 7 still matches exactly, including order. Independent review
per P4, because tool registration is an interface.

## Unit 9: `cli.py`

### Outcome and Rationale

The `serial_mcp_script` command is one top-level module that imports nothing from `mcp/` (design decision L7).

### Changes

`git mv script_cli.py cli.py`; point the `serial_mcp_script` entry point at `serial_mcp_server.cli:main`. Move
`tests/test_script_cli.py` to `tests/test_cli.py`.

### Dependencies and Constraints

Unit 7; unit 5 already moved the cancel-wait constant that `cli` used from `state.py`.

### Verification and Completion

Same test outcome; the extended no-`mcp` probe passes; `just integration` passes its command-line scenarios.
Self-review per P4.

## Unit 10: The Full Layer Contract

### Outcome and Rationale

An import against the layer order fails `just check` and CI (design section 5.2, decision L6).

### Changes

Replace the per-package contracts with a `layers` contract: `plugin_api`, then `mcp` and `cli`, then `script` and
`specs`, then `port`, then `portlog`, then `config`, `confined_files`, and `private_files`. `plugin_api` sits above
`mcp`, because it re-exports MCP handlers and state. Add an independence contract between `cli` and `plugin_api`,
and a forbidden contract that keeps `cli` from importing `serial_mcp_server.mcp`.

### Dependencies and Constraints

Units 8 and 9, so the tree matches the design.

### Verification and Completion

`just check` passes. Add a temporary import of `serial_mcp_server.mcp.state` to `port/writer.py`, confirm `just
check` fails and names the layers contract, then remove it; record both runs. Self-review per P4.

## Unit 11: Async Code on AnyIO

### Outcome and Rationale

The server's async code uses AnyIO, and the `serial.open` cleanup runs even when a cancellation lands mid-cleanup
(design section 8.4, decision L8).

### Changes

Replace each `asyncio` call by the mapping of design section 8.4. `anyio.to_thread.run_sync` waits for its thread
when cancelled unless `abandon_on_cancel=True`, unlike `asyncio.to_thread`, which abandons it; decide each call site
explicitly and record the reason. Calls that must not hold up shutdown, such as the joins under the 0.25-second
shutdown limit in `mcp/server.py`, abandon on cancel; calls whose thread changes state the caller relies on, such as
closing a port, do not. Use a shielded `CancelScope` for the `serial.open` cleanup, `fail_after` or
`move_on_after` for `wait_for`, a worker-thread wait for writer job futures, `get_cancelled_exc_class()`, and
`anyio.run` in `mcp/server.main`. Declare `anyio` as a dependency.

Replace `pytest-asyncio` with AnyIO's pytest plugin: drop it from the test extra, set `anyio_mode = "auto"` in
`[tool.pytest.ini_options]` in place of `asyncio_mode`, and run on the asyncio backend.

Write the failing test first for the cleanup gap: cancel `serial.open` while its cleanup awaits the port close, and
assert the port is closed.

### Dependencies and Constraints

Unit 10. Worker threads stay threads. `rg '\basyncio\b' serial_mcp_server` matches nothing afterwards.

### Verification and Completion

The suite passes, including the new cancellation test, which fails before the change. A server started and stopped
under the integration harness exits within its shutdown limit. `just integration` passes; its mirror-latency and
status-latency scenarios show that thread offloading still keeps the event loop free. Independent review per P4
with `model: opus`.

## Final Integration

Run the P1 commands, including `just integration`. Build the wheel with `uv build`, install it into a fresh virtual
environment outside the checkout, and check three things there: `python -m serial_mcp_server` starts and lists
tools, `serial_mcp_script --help` exits 0, and `import serial_mcp_server.port.writer` succeeds, which proves the
subpackages are in the wheel. Check the top level of `serial_mcp_server/` against design section 5.1.

Then run two separate reviews on `2a13952..HEAD`: a final `code-reviewer` pass with `model: opus` and effort
`medium` over the combined diff, and a fresh conformance review whose brief names the `conformance-review` skill,
this plan, the design, and both revisions. Record both reviews' findings and dispositions in the Execution Record;
both close before the work is done. Update the design to the as-built structure where it differs.

## Execution Record

### Plan Review

A plan-critic review on 9 October 2026 returned "proceed with changes". Fixed in this revision: the cancel-wait
constant moves into the engine (unit 5), so the command does not import the MCP package, and the probe that checks
that also covers `serial_mcp_server.mcp`; the plugin interface lands with the MCP move, so plugins change once; the
import contract starts in unit 3 and runs in CI; unit 1 names its test churn, the `SerialState` default, the
`build_server` parameter, and the trace state; the CLI's stricter start is documented; string patch targets and
`tests/` subpackage `__init__.py` files are in P2; the SDK-shadowing test can fail; `plugin_api` sits above `mcp`
in the layers; unit 11 decides `abandon_on_cancel` per call site; and unit 7 gets an independent review and a stated
`tools/list` method. Rejected: the critic's belief that AnyIO's pytest plugin has no auto mode; the installed AnyIO
4.15.1 plugin reads `anyio_mode = "auto"`.

### Unit 1: One Settings Model

Implemented on 9 October 2026. Guidance applied: `coding-standards`, `python-style`, `python-error-handling`.
`config.py` holds the `Settings` model; the field for `SERIAL_MCP_PLUGINS` is named `plugins` rather than
`plugin_policy`, and the design table follows. `load` builds the model with `Settings.model_validate`, and
`load({})` gives the defaults, which `SerialState` uses when no settings are passed. A `settings` fixture in
`tests/conftest.py` roots every test's data directory in `tmp_path`; before, `build_server` tests wrote traces into
the repository's own `.serial_mcp/`.

P1 results: 746 tests passed (704 at the base; the configuration tests added 41 and the moved environment tests
regrouped); `just check` clean; Bandit no issues; `just integration` exited 0 with every scenario passing (88 s);
`SERIAL_MCP_MIRROR=bogus SERIAL_MCP_LOG_LEVEL=loud python -m serial_mcp_server` exited 2 and named both variables.

Review: an independent `code-reviewer` approved with three optional findings, all fixed. `SERIAL_MCP_LOG_LEVEL`
accepts `CRITICAL`, as it did before; on Windows only a valid mirror mode is turned off with a warning, so an
invalid one still stops the start; and a variable with several problems gets one line in the message. The reviewer
also showed that the environment-isolation test could not fail; it now also sets the unprefixed name an
environment source would read, and a mutation that re-enables that source fails it. Not handled: a working
directory deleted before start raises `FileNotFoundError` from the data-root walk, as it did before.

### Unit 2: File Access Modules

Moved on 9 October 2026. `paths.py` became `confined_files.py` (a rename in `git diff -M`), and `private_files.py`
took `open_exclusive`, `log_base_name`, `FILE_MODE`, `DIR_MODE`, and `MAX_NAME_COLLISIONS` from `portlog.py`, which
imports them. Self-review: the diff changes only imports, patch targets, and moved code; 746 tests passed, the same
as unit 1; `just check` clean.

### Unit 3: The `port/` Package

Moved on 9 October 2026. `mirror.py`, `writer.py`, and `port_stack.py` moved into `port/` with `git mv`;
`buffer.py`, `reader.py`, `pacing.py`, and `connection.py` were carved out of them and out of `state.py` and
`handlers_serial.py`. Deviation: `create_reader` lives in `port/mirror.py`, not `port/reader.py`, because it builds a
`MirrorSession`, a subclass of `ReaderThread`, and `reader.py` importing the mirror would be a cycle; the design's
section 5.3 follows. Tests moved to `tests/port/`: `test_mirror.py` split into `test_buffer.py`, `test_reader.py`,
and `test_mirror.py`; the pacing estimate tests into `test_pacing.py`; the `SerialConnection` test into
`test_connection.py`. `import-linter` runs in `just check` and in the CI lint job, with a forbidden contract that
keeps `port` from importing the script engine, `state`, `server`, `helpers`, `script_cli`, and every handler module.

Self-review: the diff changes only imports, patch targets, and moved code. 746 tests passed, the same as unit 2;
`just check` clean, including the contract. A temporary import of `serial_mcp_server.state` in `port/writer.py`
broke the contract and named the import, and removing it restored it.

### Unit 4: The `portlog/` Package

Moved on 9 October 2026. `portlog.py` became `portlog/log.py` (moved through a temporary name, because the package
directory takes its name) and `portlog_formats.py` became `portlog/formats.py`; `portlog/request.py` took
`LogRequest`, `parse_formats`, `resolve_log_request`, `build_log_request`, and the timestamp-mode parsing.
`open_log_files` stays in `log.py`, beside `PortLog.start`, its only caller. Tests moved to `tests/portlog/`
(`test_log.py`, `test_request.py`, `test_formats.py`, `test_integration.py`); they import the modules as
`port_log` and `log_formats`, because their local variables are named `log` and `formats`. A second forbidden
contract keeps `portlog` from importing `port`, the script engine, and the MCP modules.

Self-review: the diff changes only imports, patch targets, and moved code. 746 tests passed; `just check` clean,
with both contracts kept.

### Unit 5: The Script Engine's Results and Adapter

Moved on 9 October 2026. `script/results.py` holds the run states and result types, `utc_now`, and two functions
that replace the runner's file methods: `open_result_file` creates the file and `write_result_file` writes it; the
runner records the path between the two calls, as before. Deviation: `_build_result` stays in the runner, because it
only assembles a `RunResult` from the runner's own state; design section 5.3 follows. `connection_session.py` moved
to `script/`, and `SCRIPT_CANCEL_WAIT_SECONDS` to `script/runner.py`. Tests moved to `tests/script/`; two patch
targets (`_utc_now` became `utc_now`) and one repository-relative path in `test_parser.py` changed. A third
forbidden contract keeps `script` from importing `state`, `server`, `helpers`, `script_cli`, and the handlers.

Self-review: the diff changes only imports, patch targets, and moved code. 746 tests passed; `just check` clean,
with three contracts kept. `just integration` exited 0; its scenario 5 found the same result structure from
`serial.script.run` and `serial_mcp_script`.

### Unit 6: The `specs/` Package

Moved on 9 October 2026. `specs.py` became `specs/store.py`, and `specs/frontmatter.py` took the front-matter
pattern, `parse_frontmatter`, `validate_spec_meta`, and `compute_spec_id`. `handlers_spec.py` calls `store`.
Tests moved to `tests/specs/` (`test_store.py`, `test_frontmatter.py`, and `samples.py` with the two spec texts
both use); the data-root tests, which test configuration since unit 1, moved to `tests/test_config.py`. A fourth
forbidden contract keeps `specs` from importing the port, the port log, the script engine, and the MCP modules.

Self-review: the diff changes only imports and moved code. 746 tests passed; `just check` clean, with four
contracts kept.

### Unit 7: The `mcp/` Package and the Plugin Interface

Moved on 9 October 2026. `server.py`, `state.py`, `trace.py`, and `plugins.py` moved into `mcp/`, `helpers.py`
became `mcp/responses.py`, each `handlers_<name>.py` became `mcp/tools/<name>.py`, and `mcp/writes.py` took
`duration_error`, `_bytes_written_by`, and `run_write_job`. `__main__.py` and the `serial_mcp` entry point call
`serial_mcp_server.mcp.server:main`. `plugin_api.py` re-exports `ok`, `err`, `SerialState`, `Tool`, and the four
serial read and write handlers; the plugin template and the demo plugin import from it alone, and the CHANGELOG
records the breaking change. Tests moved to `tests/mcp/` and `tests/mcp/tools/`; the spec tool tests left
`tests/specs/test_store.py` for `tests/mcp/tools/test_spec.py`, with shared helpers in `tests/specs/samples.py`.

Two earlier defects surfaced here. Unit 3's import rewrite (`2fc8aa5`) had also rewritten the import block inside
the plugin template's text, so a generated plugin did not compile; the new template replaces it, and a new test
loads a generated plugin and runs its tool. The forbidden contracts still named `state`, `server`, `helpers`, and
the handler modules, which no longer existed, and import-linter keeps such a contract without checking anything;
the contracts now name `mcp`, `plugin_api`, and `script_cli`, and `tests/test_import_contracts.py` fails when a
contract names a missing module (checked by renaming one).

P1 results: 781 tests passed (the new plugin interface, shadowing, and contract tests); `just check` clean with six
contracts kept, three of which a temporary `port/writer.py` import of `mcp.state` broke; Bandit no issues; the
extended no-`mcp` probe failed when `script_cli.py` imported `mcp.responses` and passed without it. A server started
from a worktree at `2a13952` and one started from this tree returned identical `tools/list` JSON over stdio: 38
tools, in the same order. `just integration` failed once in scenario 8b, because the catastrophic pattern
`(a|aa)+$` could match the `a` characters of a partial read; the harness fix (`8d91fcf`) uses `(a|aa)+c`, which
cannot match, and two further runs passed every scenario.

Review: an independent `code-reviewer` approved, with three optional notes. A stale comment was fixed. The
contract test was said to miss a renamed module; the rename check above shows it does not. `mcp/writes.py` has no
direct tests of its `WriterClosed`, `WriteAborted`, and cancel branches, as before the move; left as a follow-up.

### Unit 8: Serial Tools by Concern

Split on 9 October 2026. `mcp/tools/connection.py` holds port listing, open, close, configure, and connection
status, with the parity, stop-bit, and byte-size maps and `_release_when_worker_ends`; `mcp/tools/io.py` holds read,
readline, read_until, write, flush, `_format_data`, and `parse_write_pacing`; `mcp/tools/lines.py` holds the DTR and
RTS tools. `connection.py` imports `PACING_KEYS` and `parse_write_pacing` from `io.py`, never the reverse.
`mcp/server.py` concatenates connection, io, and lines where the serial module stood, and `plugin_api.py` and
`mcp/tools/file.py` import from `io.py`. The old module's unused `logger` was dropped. The tests split the same way;
each module's tests now pin its tool names in registration order instead of one count of 14.

Deviation: the old files were removed and the pieces created, not moved with `git mv`. Git records no renames, and
no piece of `serial.py` reaches the 50 percent similarity at which it reports one, so `git mv` would change nothing
in history.

P1 results: 785 tests passed (781 plus the per-module registration tests); `just check` clean with six contracts
kept. Servers started from a worktree at `945a77b` and from this tree returned identical `tools/list` JSON: 38 tools,
in the same order. A line-by-line comparison of the old and new test files differs only in the registration tests,
file headers, imports, and the formatter's wrapping of two patch targets.

Review: an independent `code-reviewer` approved with no findings.

No other unit has started.
