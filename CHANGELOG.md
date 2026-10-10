# Changelog

## Unreleased

### Changed

- Requires PyYAML 6.0.1 or later. PyYAML 6.0 no longer builds from source with current build tools, so the old minimum\
  could not be installed where no wheel exists; CI now tests the lowest version of every dependency.
- Security scanning uses GitHub CodeQL instead of Bandit. Bandit matches code patterns, and Ruff's `S` rules, which\
  already run in pre-commit and CI, are Ruff's port of those checks, so Bandit found nothing new and needed a second\
  suppression comment. CodeQL follows data flow from inputs to sensitive calls, which pattern checks cannot, and it\
  also checks the GitHub workflows.

## 0.2.1

### Fixed

- `serial.pulse_dtr` and `serial.pulse_rts` set the line high again when the request is cancelled during the pulse.\
  Before, the line stayed low, which can hold a device in reset.
- A connection opened with `timeout_ms` 0 no longer keeps a CPU core busy. `timeout_ms` now sets only the default wait\
  of the read tools; the port's own read timeout is fixed at 200 ms, so the reader thread also stops promptly when a\
  connection with a long `timeout_ms` closes. A plugin that reads the port object directly waits at most 200 ms per\
  read.

## 0.2.0

The first release from this fork of es617/serial-mcp-server, which it extends from upstream's 0.1.3. It adds write\
pacing, port logs, file sending, and a serial script runner with a command-line runner, and changes the plugin\
interface. Releases are published on GitHub only; install from this repository.

### Added

- Write pacing for slow receivers: `inter_char_delay_ms`, or `chunk_size` with `chunk_delay_ms`, on `serial.open` for\
  the connection, and on `serial.write` and `serial.send_file` for one call. Input from a read/write mirror follows the\
  connection's pacing. Cancelling the request stops a paced write between segments.
- Port log: set `SERIAL_MCP_LOG=1`, or pass `log` or `log_formats` to `serial.open`, to record the bytes on the wire in\
  `text` (received bytes unchanged, with optional `grabserial`-style timestamps), `jsonl` (both directions plus control\
  events), `lines` (one entry per line, with partial prompts flagged), and `raw` (received bytes, byte-exact) files\
  under `SERIAL_MCP_LOG_DIR` (default `.serial_mcp/logs/`). Off by default; files are mode `0600`. See\
  [docs/tools.md](docs/tools.md#serialopen).
- `serial.log.start`, `serial.log.stop`, `serial.log.status`, and `serial.log.mark` start, stop, inspect, and annotate\
  the port log of an open connection. `serial.connection_status` now reports `dropped_bytes`, the received bytes the\
  read buffer discarded because nothing read them, and each drop is recorded as a `buffer_drop` event in the port log.
- `serial.send_file` sends a prepared file from `SERIAL_MCP_FILES_DIR` (default `.serial_mcp/files/`) to the device\
  with the connection's pacing. Paths that leave the directory, directories, FIFOs, links that point outside it, and\
  files over 1 MiB are refused before any byte is sent.
- `serial.configure` changes write pacing, encoding, and newline on an open connection without reopening it, which\
  avoids a DTR or RTS toggle that can reset the device.
- `SERIAL_MCP_MAX_WRITE_SECONDS` (default 60) caps the estimated duration of a paced `serial.write` and of any\
  `serial.send_file` transfer. The estimate is the larger of the pauses between segments and the time on the wire at\
  the connection's baud rate (ten bits per byte). Longer writes are rejected with `invalid_params` before any byte is\
  sent.
- Serial scripts send, expect, and prompt on a connection in the background, and each run writes a private JSON result\
  under `SERIAL_MCP_LOG_DIR`. Agents use them through `serial.script.run`, `serial.script.status`,\
  `serial.script.cancel`, `serial.script.list`, and `serial.script.template`. Script files must stay inside\
  `SERIAL_MCP_SCRIPTS_DIR` (default `.serial_mcp/scripts/`), and inline source is limited to 256 KiB. A running script\
  owns its connection's writes and control lines. The tool-call trace strips the `source` and `vars` arguments, as it\
  strips write `data`, from every tool, plugin tools included. See [docs/scripts.md](docs/scripts.md).
- `SERIAL_MCP_SCRIPT_MAX_SECONDS` (default `3600`) caps each script run. Each pattern search is limited to two seconds\
  unless `SERIAL_MCP_SCRIPT_PATTERN_SECONDS` sets another, so a pattern that backtracks without end ends the run\
  `errored` with the reason `regex_timeout` instead of freezing the server.
- `serial_mcp_script` runs one serial script on a port with no agent and no MCP server, and reports the outcome through\
  its exit status (`0` passed, `1` failed, `2` error or unusable input, `130` interrupted) and a result file. See\
  [Running from the command line](docs/scripts.md#running-from-the-command-line).
- [docs/plugins.md](docs/plugins.md) explains plugins, and [docs/configuration.md](docs/configuration.md) lists every\
  variable and the data directory.

### Changed

- **Breaking for plugins:** plugins import from `serial_mcp_server.plugin_api` only. The server's modules moved into\
  packages (`mcp/`, `port/`, `portlog/`, `script/`, `specs/`), so `serial_mcp_server.helpers`,\
  `serial_mcp_server.state`, and `serial_mcp_server.handlers_serial` no longer exist, and a plugin that imports them\
  fails to load: the server logs the import error at start, and `serial.plugin.load` returns it as `plugin_error`.\
  Replace `from serial_mcp_server.helpers import _ok, _err`, `from serial_mcp_server.state import SerialState`, and\
  `from serial_mcp_server.handlers_serial import handle_write, ...` with\
  `from serial_mcp_server.plugin_api import ok, err, SerialState, handle_write, ...`, and call `ok`/`err` instead of\
  `_ok`/`_err`. Plugins generated by `serial.plugin.template` now follow this. `SerialState` now takes the server's\
  `Settings` instead of `max_connections`, so read the connection limit from `state.settings.max_connections`.
- Requires `mcp` 1.30 or later, which carries the tool annotations clients use to decide what to ask before running a\
  tool. New runtime dependencies: `anyio`, `regex`, and `pydantic-settings`.
- Configuration is read once, when the server or `serial_mcp_script` starts, into one validated set of settings;\
  changing the process environment afterwards has no effect. An invalid `SERIAL_MCP_*` value now stops the start with\
  exit status 2 and one message that names every invalid variable. Before, the outcome depended on the variable:\
  `SERIAL_MCP_MAX_CONNECTIONS` and `SERIAL_MCP_TRACE_MAX_BYTES` crashed with a traceback, `SERIAL_MCP_MIRROR` and\
  `SERIAL_MCP_MAX_WRITE_SECONDS` logged a warning and used the default, `SERIAL_MCP_LOG_LEVEL` silently used `WARNING`\
  for an unknown name (it now accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`), and\
  `SERIAL_MCP_SCRIPT_MAX_SECONDS` and the log variables failed each call that used them. `serial_mcp_script` also stops\
  on an invalid variable that only the server uses, such as `SERIAL_MCP_MIRROR`. See the\
  [environment variables](docs/configuration.md#environment-variables).
- Every boolean variable accepts `1`, `true`, `yes`, `on`, `0`, `false`, `no`, and `off` in any case.\
  `SERIAL_MCP_TRACE=off` now disables tracing; before, only `0`, `false`, and `no` did.
- Every tool description now opens with when to use the tool, and every property has a description. `serial.configure`,\
  `serial.set_dtr`, `serial.set_rts`, `serial.spec.register`, and `serial.spec.attach` carry `idempotentHint`, and\
  `serial.spec.register` and `serial.spec.attach` are no longer marked destructive, so clients that ask before\
  destructive tools stop asking for them.
- The schema defaults of `newline` on `serial.open` and `delimiter` on `serial.read_until` are now the real characters\
  (`\r\n` and `\n`) that the server uses, not their backslash spellings.
- The package's project URLs (homepage, repository, issues, changelog) now point to
  <https://github.com/jonatsu/serial-mcp-server>.
- `serial.write` returns `write_aborted` with `bytes_written` when a write stops early or times out partway. A write\
  still queued when the connection closes returns `connection_closed`, and `serial.flush` with `output` or `both` also\
  discards queued writes.
- Releases are GitHub releases of this repository, with the wheel and source archive attached. Nothing is published to\
  PyPI or the MCP registry, whose `serial-mcp-server` entries remain upstream's, and `server.json` is removed.

### Fixed

- One Ctrl-C (SIGINT) now stops the server, which closes its connections and port logs and exits 0. Before, it\
  stayed up until the client also closed stdin, because the SDK read stdin in a thread that nothing could interrupt.
- A spec index (`.serial_mcp/index.json`) that cannot be read is reported as `index_corrupt` by the spec tools and\
  left untouched. Before, it read as empty, and the next `serial.spec.register` replaced it, losing every other\
  registration.
- In `SERIAL_MCP_MIRROR=rw` mode, input from the mirror terminal no longer stalls receiving. The mirror's reader thread\
  wrote that input to the port itself, so a slow write, or an MCP write holding the port, delayed device output and\
  could let the kernel buffer overflow. Each connection now has one writer thread that runs all writes in order.
- Errors while forwarding mirror input to the serial port are now logged instead of silently dropped.
- With `SERIAL_MCP_TOOL_SEPARATOR` set, tool descriptions, property descriptions, result hints, and error messages now\
  name the tools by their renamed names, such as `serial_spec_list`. Before, only the tool names changed, so the text\
  pointed agents at tools that did not exist.
- A cancelled `serial.open` always releases the port before the request ends. Before, a cancellation that arrived while\
  the port was opening left the port to garbage collection, and one that arrived during the cleanup of a failed open\
  could skip closing it.

## 0.1.3

### Fixed

- Raise minimum `mcp` SDK dependency to >=1.23.0 to exclude versions with known CVEs (CVE-2025-53366, CVE-2025-53365,\
  CVE-2025-66416). These affect HTTP/SSE transport only — stdio servers were never vulnerable — but the wider range\
  allowed scanners to flag the package.

## 0.1.2

### Added

- VS Code / Copilot setup instructions in README (`.vscode/mcp.json`)
- Cursor setup instructions in README (`.cursor/mcp.json`)
- `SERIAL_MCP_TOOL_SEPARATOR` env var — configurable separator for tool names (default `.`). Set to `_` for MCP clients\
  that reject dots in tool names (e.g. Cursor).

## 0.1.1

### Fixed

- Accept string-typed numeric parameters in tool schemas (`"9600"` instead of `9600`). Some MCP clients serialize all\
  tool arguments as strings, which caused JSON Schema validation errors on `integer` and `number` fields. Affected\
  fields: `baudrate`, `bytesize`, `stopbits`, `timeout_ms`, `write_timeout_ms`, `nbytes`, `max_bytes`, `duration_ms`,\
  `k`, `n`.

## 0.1.0

Initial release.

### Serial Core

- List available serial ports with device metadata (VID, PID, manufacturer, serial number)
- Open connections with configurable baud rate, byte size, parity, stop bits, timeout, and encoding
- Close connections, query connection status
- Read data with configurable byte count and timeout override
- Write data in text, hex, or base64 format, with optional newline append
- Line-oriented I/O: `serial.readline` and `serial.read_until` with custom delimiters
- Flush input/output buffers (or both)
- Control lines: set and pulse DTR/RTS (useful for hardware reset and boot mode entry)
- Duplicate port detection (rejects opening the same port twice)
- Graceful shutdown (closes all serial ports on exit)

### PTY Mirror

- Virtual clone ports via pseudo-terminals (`SERIAL_MCP_MIRROR=ro` or `rw`)
- External tools (screen, picocom, etc.) can attach to the same serial session
- Default symlink at `/tmp/serial-mcp0`, configurable via `SERIAL_MCP_MIRROR_LINK`
- macOS and Linux only

### Introspection

- `serial.connections.list` for recovering connection IDs and inspecting state

### Protocol Specs

- Markdown specs with YAML front-matter (`kind: serial-protocol`, `name`)
- Template generation, registration, indexing
- Attach specs to connections for agent reference
- Full-text search over spec content

### Tracing

- JSONL tracing of every tool call (in-memory ring buffer + file sink)
- Configurable payload logging with truncation
- `serial.trace.status` and `serial.trace.tail` for inspection

### Plugins

- User plugins in `.serial_mcp/plugins/` (single files or packages)
- Plugin contract: `TOOLS`, `HANDLERS`, optional `META` for device matching
- `SERIAL_MCP_PLUGINS` env var: `all` or comma-separated allowlist
- `serial.plugin.template` for generating plugin skeletons
- `serial.plugin.list` with metadata, `serial.plugin.load`, `serial.plugin.reload`
- Hot-reload without server restart

### Security

- Plugin path containment: `serial.plugin.load` rejects paths outside `.serial_mcp/plugins/`
- Spec path containment: `serial.spec.register` rejects paths outside the project directory
- Trace file always writes to `.serial_mcp/traces/trace.jsonl` (no configurable path)
- Symlink check on trace file path
- Input validation for hex/base64 write payloads
