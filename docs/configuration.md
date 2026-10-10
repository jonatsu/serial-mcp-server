# Configuration

The server and the `serial_mcp_script` command take their settings from `SERIAL_MCP_*` environment variables. Set them\
where the MCP client starts the server: `claude mcp add -e NAME=value`, or the `env` block of `.vscode/mcp.json` or\
`.cursor/mcp.json` (see [the README](../README.md#add-to-claude-code)). Every variable is optional.

## Environment variables

The server and `serial_mcp_script` read these variables once, when they start. An unset or blank variable means its\
default. A boolean accepts `1`, `true`, `yes`, or `on`, and `0`, `false`, `no`, or `off`, in any case. Any other\
invalid value stops the start with exit status 2 and a message that names every invalid variable, so one start reports\
every mistake.

| Variable | Default | Description |
| -- | -- | -- |
| `SERIAL_MCP_SPEC_ROOT` | `.serial_mcp/` | The data directory that holds specs, plugins, scripts, files, logs, and traces. By default, the first `.serial_mcp/` from the working directory upwards, else one next to the nearest `.git/`, else one in the working directory. The directory variables below default to subdirectories of it. |
| `SERIAL_MCP_MAX_CONNECTIONS` | `10` | Maximum simultaneous open serial connections. |
| `SERIAL_MCP_PLUGINS` | disabled | Plugin policy: `all` to allow all, or `name1,name2` to allow specific plugins. Unset = disabled. |
| `SERIAL_MCP_MIRROR` | `off` | PTY mirror mode: `off`, `ro` (read-only), or `rw` (read-write). macOS and Linux only. |
| `SERIAL_MCP_MIRROR_LINK` | `/tmp/serial-mcp` | Base path for PTY symlinks. Connections get numbered: `/tmp/serial-mcp0`, `/tmp/serial-mcp1`, etc. |
| `SERIAL_MCP_MAX_WRITE_SECONDS` | `60` | Cap on the estimated duration of a paced `serial.write` and of any `serial.send_file` transfer. The estimate is the larger of the pauses between segments and the time on the wire (ten bits per byte at the connection's baud rate). Longer writes are rejected with `invalid_params` before any byte is sent. Script writes are not subject to it. |
| `SERIAL_MCP_SCRIPT_MAX_SECONDS` | `3600` | Maximum duration of one script run. A run that exceeds it fails with `max_duration`. |
| `SERIAL_MCP_SCRIPTS_DIR` | `.serial_mcp/scripts/` | Directory for script files used by `serial.script.run` and `serial.script.list`. |
| `SERIAL_MCP_FILES_DIR` | `.serial_mcp/files/` | The only directory `serial.send_file` reads from. Files must be regular and at most 1 MiB. Do not put hard links or bind mounts to sensitive files in it: the server cannot tell them from regular files. |
| `SERIAL_MCP_LOG` | disabled | Set to a true value, such as `1`, to record every connection's bytes in a port log. `serial.open` can override it per connection with `log`. |
| `SERIAL_MCP_LOG_FORMATS` | `text` | Comma-separated port log formats (`text`, `jsonl`, `lines`, `raw`) used when a connection does not choose its own. |
| `SERIAL_MCP_LOG_TEXT_TIMESTAMPS` | `off` | Prefix each line of the `text` log with `[time delta]`: `off`, `elapsed`, or `system`. `serial.open` can override it with `log_text_timestamps`. |
| `SERIAL_MCP_LOG_DIR` | `.serial_mcp/logs/` | Directory for port log and script result files. Files are created with mode `0600`. The server never deletes them, and every `serial.script.run` adds a result file and, when the connection has no active log, a port log. Point this at a directory you clean. There is no log rotation yet; defining one is future work. |
| `SERIAL_MCP_LOG_LEVEL` | `WARNING` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). Logs go to stderr. |
| `SERIAL_MCP_TRACE` | enabled | JSONL tracing of every tool call. Set to a false value, such as `0` or `off`, to disable. |
| `SERIAL_MCP_TRACE_PAYLOADS` | disabled | Include write `data`, and the `source` and `vars` of `serial.script.run`, in traced args (stripped by default). |
| `SERIAL_MCP_TRACE_MAX_BYTES` | `16384` | Max payload chars before truncation (only applies when `TRACE_PAYLOADS` is on). |
| `SERIAL_MCP_TOOL_SEPARATOR` | `.` | Character used to separate tool name segments. Set to `_` for MCP clients that reject dots in tool names (e.g. Cursor). |

## The data directory

The server keeps its files in one data directory, `.serial_mcp/` by default, which `SERIAL_MCP_SPEC_ROOT` moves.\
Without that variable, the server uses the first `.serial_mcp/` it finds from the working directory upwards, else one\
next to the nearest `.git/`, else one in the working directory.

```text
.serial_mcp/
├── specs/      # protocol specs, and index.json, the spec index
├── plugins/    # plugins, loaded when SERIAL_MCP_PLUGINS allows them
├── scripts/    # script files for serial.script.run (SERIAL_MCP_SCRIPTS_DIR)
├── files/      # files that serial.send_file may send (SERIAL_MCP_FILES_DIR)
├── logs/       # port logs and script results (SERIAL_MCP_LOG_DIR)
└── traces/     # trace.jsonl, the tool-call trace
```

Specs, plugins, scripts, and files are project material worth keeping in version control. Port logs, script results,\
and the trace hold device output and what was sent to the device, so keep `logs/` and `traces/` out of it; the server\
never deletes those files.
