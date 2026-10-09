# Changelog

## Unreleased

### Added
- Port log: set `SERIAL_MCP_LOG=1`, or pass `log` or `log_formats` to `serial.open`, to record the bytes on the wire in `text` (received bytes unchanged, with optional `grabserial`-style timestamps), `jsonl` (both directions plus control events), `lines` (one entry per line, with partial prompts flagged), and `raw` (received bytes, byte-exact) files under `SERIAL_MCP_LOG_DIR` (default `.serial_mcp/logs/`). Off by default; files are mode `0600`. See [docs/tools.md](docs/tools.md#serialopen).
- `serial.log.start`, `serial.log.stop`, `serial.log.status`, and `serial.log.mark` start, stop, inspect, and annotate the port log of an open connection. `serial.connection_status` now reports `dropped_bytes`, the received bytes the read buffer discarded because nothing read them, and each drop is recorded as a `buffer_drop` event in the port log.
- `serial.send_file` sends a prepared file from `SERIAL_MCP_FILES_DIR` (default `.serial_mcp/files/`) to the device with the connection's pacing. Paths that leave the directory, directories, FIFOs, links that point outside it, and files over 1 MiB are refused before any byte is sent.
- `serial.configure` changes write pacing, encoding, and newline on an open connection without reopening it, which avoids a DTR or RTS toggle that can reset the device.
- `SERIAL_MCP_MAX_WRITE_SECONDS` (default 60) caps the estimated duration of a paced `serial.write` and of any `serial.send_file` transfer. The estimate is the larger of the pauses between segments and the time on the wire at the connection's baud rate (ten bits per byte). Longer writes are rejected with `invalid_params` before any byte is sent.
- The script engine runs validated serial scripts on a dedicated thread and writes private JSON results under `SERIAL_MCP_LOG_DIR`. `SERIAL_MCP_SCRIPT_MAX_SECONDS` (default `3600`) caps each run. Patterns use the `regex` package, and each search has a two-second limit, so a pattern that backtracks without end ends the run `errored` with the reason `regex_timeout` instead of freezing the server. See [docs/scripts.md](docs/scripts.md).
- Agents can run, inspect, cancel, list, and request a template for serial scripts through five MCP tools. Script files must stay inside `SERIAL_MCP_SCRIPTS_DIR` (default `.serial_mcp/scripts/`). Inline source is limited to 256 KiB. A running script owns its connection's writes and control lines. The tool-call trace strips the inline `source` and the `vars`, as it strips write `data`; it strips an argument with either name from any tool, plugin tools included. See [docs/scripts.md](docs/scripts.md).
- `serial_mcp_script` runs one serial script on a port with no agent and no MCP server, and reports the outcome through its exit status (`0` passed, `1` failed, `2` error or unusable input, `130` interrupted) and a result file. See [Running from the command line](docs/scripts.md#running-from-the-command-line).

### Changed
- The package's project URLs (homepage, repository, issues, changelog) now point to <https://github.com/jonatsu/serial-mcp-server>.

### Fixed
- Paced writes no longer stall receiving in `SERIAL_MCP_MIRROR=rw` mode. All writes now go through one writer thread per connection, so pasting into the mirror terminal cannot delay device output or cause the kernel buffer to overflow.
- Errors while forwarding mirror input to the serial port are now logged instead of silently dropped.
- `serial.write` after a connection closes returns `connection_closed`, and `serial.flush` with `output` or `both` also discards writes still queued.
- `serial.write` returns `write_aborted` with `bytes_written` when a write stops early or times out partway, and cancelling the request stops a paced write between segments.

## 0.1.3

### Fixed
- Raise minimum `mcp` SDK dependency to >=1.23.0 to exclude versions with known CVEs (CVE-2025-53366, CVE-2025-53365, CVE-2025-66416). These affect HTTP/SSE transport only — stdio servers were never vulnerable — but the wider range allowed scanners to flag the package.

## 0.1.2

### Added
- VS Code / Copilot setup instructions in README (`.vscode/mcp.json`)
- Cursor setup instructions in README (`.cursor/mcp.json`)
- `SERIAL_MCP_TOOL_SEPARATOR` env var — configurable separator for tool names (default `.`). Set to `_` for MCP clients that reject dots in tool names (e.g. Cursor).

## 0.1.1

### Fixed
- Accept string-typed numeric parameters in tool schemas (`"9600"` instead of `9600`). Some MCP clients serialize all tool arguments as strings, which caused JSON Schema validation errors on `integer` and `number` fields. Affected fields: `baudrate`, `bytesize`, `stopbits`, `timeout_ms`, `write_timeout_ms`, `nbytes`, `max_bytes`, `duration_ms`, `k`, `n`.

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
