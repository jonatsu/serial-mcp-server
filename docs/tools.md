# Tools Reference

All tools return structured JSON: `{ "ok": true, ... }` on success,\
`{ "ok": false, "error": { "code": "...", "message": "..." } }` on failure.

Built-in tools provide MCP tool annotations so clients can classify their behavior. These are hints; each client\
decides whether to use a tool automatically or request approval. Serial read tools consume the shared buffer, so they\
are not marked read-only. Plugin tools keep the annotations supplied by their authors.

---

## Serial Core

### serial.list_ports

List available serial ports on the system.

```json
{}
```

Returns:

```json
{
  "ok": true,
  "message": "Found 2 serial port(s).",
  "ports": [
    {
      "device": "/dev/ttyUSB0",
      "name": "ttyUSB0",
      "description": "USB Serial",
      "hwid": "USB VID:PID=1234:5678",
      "vid": "0x1234",
      "pid": "0x5678",
      "serial_number": "ABC123",
      "manufacturer": "FTDI",
      "product": "FT232R"
    }
  ],
  "count": 2
}
```

Fields like `vid`, `pid`, `serial_number`, `manufacturer`, and `product` are included when available.

### serial.open

Open a serial port connection. Returns a `connection_id` for use with other tools. Defaults are 115200 baud, 8N1,\
`\r\n` line terminator.

```json
{
  "port": "/dev/ttyUSB0",
  "baudrate": 115200,
  "bytesize": 8,
  "parity": "N",
  "stopbits": 1,
  "timeout_ms": 200,
  "write_timeout_ms": 200,
  "inter_char_delay_ms": 0,
  "chunk_size": 0,
  "chunk_delay_ms": 0,
  "encoding": "utf-8",
  "newline": "\r\n"
}
```

Only `port` is required. All other parameters have defaults.

Set `inter_char_delay_ms` from 0 to 1000 to pause between transmitted characters. The default is `0` (no pacing). This\
connection setting applies to `serial.write` and writes forwarded through a read/write PTY mirror. Alternatively, set\
both `chunk_size` and `chunk_delay_ms` to positive values to pause after each chunk drains. Only one pacing mode may be\
active. Both chunk settings default to `0`.

Set `log` to `true` to record the bytes on the wire in a port log, and `log_formats` to choose the file formats.\
Passing `log_formats` alone also enables logging, and `log: false` disables it whatever the environment says. The\
formats are:

| Format | Extension | Content |
| -- | -- | -- |
| `text` | `txt` | Received bytes written unchanged, so lines split where the device sent newlines. Default. |
| `jsonl` | `jsonl` | One JSON object per received or transmitted chunk, plus control events. |
| `lines` | `lines.jsonl` | One JSON object per newline-terminated line in each direction, stamped when the line's first byte arrived. The text omits the trailing `\r\n` or `\n`. |
| `raw` | `bin` | Received bytes only, byte-exact, for replay and binary diffing. |

A `lines` entry looks like\
`{"ts": "2026-10-08T12:00:00.123456Z", "t": 1.234567, "dir": "rx", "text": "U-Boot 2024.01"}`. A line with no newline\
after 500 ms of silence is written with `"partial": true`, so a prompt such as `=> ` still appears. Bytes that arrive\
later on the same line start a new entry with `"continued": true`, so joining a partial entry with its continuations\
restores the line. A `mark` event written by the log tools keeps its place between lines.

Set `log_text_timestamps` to `elapsed` or `system` to prefix each line of the `text` log in the style of `grabserial`,\
a serial capture tool by Tim Bird: `[<time> <delta>] `, stamped when the line's first byte arrives. With `elapsed`,\
`<time>` is seconds since the first received byte; with `system` it is the UTC wall-clock time. `<delta>` is seconds\
since the previous line, and both numbers have six decimal places. The default is `off`, so the `text` file holds\
exactly the received bytes. Transmitted bytes never appear in the `text` file.

The agent never chooses the directory or file names. Files go to `SERIAL_MCP_LOG_DIR` (default `.serial_mcp/logs/`) and\
are named `serial-mcp-log-<DDMMYYYY>-<HHMM>[-<n>].<extension>` in local time; all formats of one log share the name.\
Files are created with mode `0600`, because a console log can capture passwords. If the log cannot be started, the port\
is closed and the call returns `log_failed`. The server never deletes log files or script result files, and each\
`serial.script.run` adds a result file and, when the connection has no active log, a port log. Point\
`SERIAL_MCP_LOG_DIR` at a directory you clean. There is no log rotation yet; defining one is future work. A `jsonl`\
entry looks like this:

```json
{
  "ts": "2026-10-08T12:00:00.123456Z",
  "t": 1.234567,
  "dir": "tx",
  "source": "mcp",
  "len": 3,
  "hex": "6c730a",
  "text": "ls\n"
}
```

The `source` is `mcp` or `mirror`. Other entries have an `event` field instead of `dir`: `open`, `close`, `dtr`, `rts`,\
`flush`, `configure`, `mark`, `buffer_drop`, `send_file_start`, `send_file_end`, `script_start`, `script_end`, and\
`log_overflow`. If the disk falls more than 8 MiB behind, the log drops entries rather than block the port, and\
`serial.log.status` and `serial.log.stop` report the dropped byte count as `dropped_bytes`. The formats record the drop\
differently. The `jsonl` and `lines` files get a `log_overflow` event with the exact byte count, and the `text` file\
gets a `--- serial-mcp: <n> bytes not logged ---` line. The `raw` file records nothing, so a gap in it is silent; check\
`dropped_bytes`. When logging, the response and `serial.connection_status` include `log`, with the `formats` and the\
`files` paths. A port log holds everything on the wire, including passwords typed into a console and any terminal\
control sequences the device sends. Treat the files as sensitive, and view them with a pager that does not interpret\
escape sequences. `serial.open` also rejects an `encoding` that is not a text codec, such as `hex`.

Returns:

```json
{
  "ok": true,
  "message": "Opened /dev/ttyUSB0 at 115200 baud.",
  "connection_id": "s1a2b3c4",
  "config": {
    "port": "/dev/ttyUSB0",
    "baudrate": 115200,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "timeout_ms": 200,
    "write_timeout_ms": 200,
    "inter_char_delay_ms": 0,
    "chunk_size": 0,
    "chunk_delay_ms": 0,
    "encoding": "utf-8",
    "newline": "'\\r\\n'"
  },
  "mirror": {
    "pty_path": "/dev/ttys004",
    "link": "/tmp/serial-mcp0",
    "mode": "ro"
  }
}
```

The `mirror` field is only present when `SERIAL_MCP_MIRROR` is set to `ro` or `rw`.

### serial.close

Close a serial port connection and release the port.

```json
{
  "connection_id": "s1a2b3c4"
}
```

### serial.connection_status

Check whether a serial connection is still open and return its configuration.

```json
{
  "connection_id": "s1a2b3c4"
}
```

Returns `{ "ok": true, "is_open": true, "config": { ... }, "buffered_bytes": 0, "dropped_bytes": 0 }`. Includes\
`mirror` when active and `log` when logging.

`dropped_bytes` counts received bytes the 1 MiB read buffer discarded because nothing read them in time. A value above\
zero means earlier output is lost, so read more often. Each drop also records a `buffer_drop` event in the port log.

### serial.configure

Change write pacing, text encoding, or the newline of an open connection without reopening it. Reopening can toggle DTR\
and RTS and reset the device, so use this tool instead.

```json
{
  "connection_id": "s1a2b3c4",
  "chunk_size": 16,
  "chunk_delay_ms": 20,
  "newline": "\r\n"
}
```

Pass at least one of `inter_char_delay_ms`, `chunk_size`, `chunk_delay_ms`, `encoding`, and `newline`. The pacing\
arguments replace the whole pacing mode, as a per-write override on `serial.write` does, so\
`{"inter_char_delay_ms": 8}` also clears any chunk pacing and `{"inter_char_delay_ms": 0}` turns pacing off. The server\
validates every argument first; an invalid one returns `invalid_params` and leaves the connection unchanged.

A change applies to writes submitted afterwards, including PTY mirror input. Writes already submitted keep their\
settings. A new `newline` becomes the default terminator for `serial.readline` and for `append_newline` on\
`serial.write`.

Returns `{ "ok": true, "config": { ... } }` in the same shape as `serial.connection_status`.

### serial.read

Read up to `nbytes` from a serial port. Returns as soon as any bytes are buffered, or an empty result once the read\
timeout passes.

```json
{
  "connection_id": "s1a2b3c4",
  "nbytes": 256,
  "timeout_ms": 500,
  "as": "text"
}
```

Only `connection_id` is required. `as` can be `"text"` (default), `"hex"`, or `"base64"`.

Returns:

```json
{
  "ok": true,
  "message": "Read 12 byte(s) from /dev/ttyUSB0.",
  "n_read": 12,
  "data": "Hello world\n",
  "format": "text",
  "encoding": "utf-8"
}
```

### serial.write

Write data to a serial port.

```json
{
  "connection_id": "s1a2b3c4",
  "data": "AT+VERSION",
  "append_newline": true
}
```

- `as`: `"text"` (default), `"hex"`, or `"base64"` — how to interpret the `data` string
- `append_newline`: append the connection's newline (`\r\n` by default) after data
- `inter_char_delay_ms`: optional delay between transmitted characters, from 0 to 1000 milliseconds.
- `chunk_size` and `chunk_delay_ms`: optional positive byte count and delay in milliseconds, up to 1000 ms. Both must\
  be set to use chunk pacing.

A write uses the connection's pacing settings unless it specifies any pacing option. A per-write option replaces the\
complete connection pacing mode. Set `inter_char_delay_ms` to `0` on a write to disable pacing for that call. Character\
and chunk pacing cannot both be active.

For example, `{"connection_id": "s1a2b3c4", "data": "AT+VERSION", "chunk_size": 4, "chunk_delay_ms": 8}` writes at most\
four bytes per chunk, with an 8 ms pause after each drained chunk except the last.

`bytes_written` counts bytes accepted by the host serial layer. It does not confirm that the receiving device got them.\
A delay of 8 ms per character can help receivers that overrun during long writes, at the cost of longer write times.

Returns `{ "ok": true, "message": "Wrote 12 byte(s) to /dev/ttyUSB0.", "bytes_written": 12 }`.

Writes to one connection run in order on a single writer thread, so concurrent writes and mirror input never interleave\
within a payload. If the connection closes while a write is queued, the call returns the error code\
`connection_closed`. If a write stops early because `serial.flush` discarded it, or because the connection closed\
mid-write, the call returns the error code `write_aborted` and a `bytes_written` count. Cancelling the request stops a\
paced write between segments and returns no result.

A paced write is rejected with `invalid_params` before any byte is sent when its estimated duration exceeds\
`SERIAL_MCP_MAX_WRITE_SECONDS` (default 60). The estimate is the larger of the pauses between segments (the number of\
segments times the delay) and the time the bytes take on the wire at the connection's baud rate, with ten bits per\
byte. A write with no pacing is not capped. Time spent queued behind other writes is not counted. A write timeout\
partway through returns `write_aborted` with `bytes_written` set to the bytes of completed segments, which is a lower\
bound because the host may have accepted part of the failed segment.

### serial.send_file

Send a prepared file to the device, with the connection's pacing. Use it for long inputs, such as a U-Boot environment\
or a shell script, so the test input lives in a file rather than in tool arguments.

```json
{
  "connection_id": "s1a2b3c4",
  "path": "uboot/env.txt",
  "chunk_size": 16,
  "chunk_delay_ms": 20
}
```

`path` is relative to the files directory, `SERIAL_MCP_FILES_DIR` (default `.serial_mcp/files/`). The server reads only\
regular files strictly inside that directory, so the agent cannot send other files, such as an SSH key, over the serial\
line. It refuses:

- an absolute path, a path that leaves the directory through `..`, a symbolic link that points outside it, and the\
  directory itself;
- a directory, a FIFO, or a device file;
- a file over 1 MiB.

Each refusal returns `invalid_params` before any byte is sent; a missing target is one of them. A file that exists but\
cannot be read returns `file_unreadable`. The server opens the file without following links and checks its type and\
size again on the open handle, so swapping a file for a link or FIFO between the check and the read does not help. A\
hard link or a bind mount that an operator places inside the files directory looks like a regular file there, so do not\
link sensitive files into it.

The pacing arguments override the connection's, as on `serial.write`. Without pacing, the file goes out in 4 KiB\
segments. The estimated duration must fit the `SERIAL_MCP_MAX_WRITE_SECONDS` cap: it is the larger of the pauses\
between segments (the number of segments times the delay) and the time the bytes take on the wire at the connection's\
baud rate with ten bits per byte. A transfer that stops early returns `write_aborted` with `bytes_written`, as\
`serial.write` does.

With a port log active, the log records `send_file_start` and `send_file_end` events with that path, and the transfer's\
`tx` entries sit between them, even when the transfer is cancelled.

Returns `{ "ok": true, "bytes_written": 1234, "path": "uboot/env.txt" }`. The path is relative to the files directory,\
so neither the response nor the log shows the operator's directory layout.

### serial.readline

Read a line from the serial port (reads until the newline character is received or `max_bytes` is reached). Uses the\
connection's newline setting by default.

```json
{
  "connection_id": "s1a2b3c4",
  "timeout_ms": 1000,
  "max_bytes": 4096
}
```

Only `connection_id` is required. Supports `as` and `newline` overrides.

Returns `{ "ok": true, "n_read": 15, "data": "OK 200 ready\r\n", "format": "text" }`.

### serial.read_until

Read from the serial port until a delimiter string is received or `max_bytes` is reached.

```json
{
  "connection_id": "s1a2b3c4",
  "delimiter": ">",
  "max_bytes": 4096
}
```

Only `connection_id` is required. Default delimiter is `\n`.

### serial.flush

Flush serial port buffers (discard pending data).

```json
{
  "connection_id": "s1a2b3c4",
  "what": "both"
}
```

`what` can be `"input"`, `"output"`, or `"both"` (default). Flushing output also cancels writes still queued; they\
return `write_aborted`.

### serial.set_dtr

Set the DTR (Data Terminal Ready) control line. Usage is device-specific.

```json
{
  "connection_id": "s1a2b3c4",
  "value": false
}
```

### serial.set_rts

Set the RTS (Request To Send) control line. Usage is device-specific.

```json
{
  "connection_id": "s1a2b3c4",
  "value": true
}
```

### serial.pulse_dtr

Pulse the DTR line: sets low, waits `duration_ms`, then sets high. Commonly used to reset microcontrollers. A pulse\
that began before a script run claimed the connection still sets the line high when it ends.

```json
{
  "connection_id": "s1a2b3c4",
  "duration_ms": 100
}
```

Only `connection_id` is required. Default duration is 100ms.

### serial.pulse_rts

Pulse the RTS line: sets low, waits `duration_ms`, then sets high. Some devices use RTS to enter bootloader mode. A\
pulse that began before a script run claimed the connection still sets the line high when it ends.

```json
{
  "connection_id": "s1a2b3c4",
  "duration_ms": 100
}
```

---

## Port Log

These tools start, stop, inspect, and annotate the port log of an open connection. `serial.open` can also start a log\
at open time; its section describes the formats, file names, and what each file holds.

### serial.log.start

Start recording the connection's bytes on the wire in new files.

```json
{
  "connection_id": "s1a2b3c4",
  "log_formats": [
    "text",
    "jsonl"
  ],
  "log_text_timestamps": "elapsed"
}
```

Only `connection_id` is required. The formats default to `SERIAL_MCP_LOG_FORMATS`, then `text`. The first `jsonl` event\
is `open`, with the connection's configuration. A connection that is already logging returns `already_logging`; call\
`serial.log.stop` first. If the files cannot be created, the call returns `log_failed`.

Returns `{ "ok": true, "log": { "formats": [...], "files": { "text": "<path>" }, "entries": 0, "dropped_bytes": 0 } }`.

### serial.log.stop

Flush and close the log files. The connection stays open, and a later `serial.log.start` begins new files.

```json
{
  "connection_id": "s1a2b3c4"
}
```

Returns\
`{ "ok": true, "files": [{ "format": "text", "path": "<path>", "bytes": 1234 }], "entries": 12, "dropped_bytes": 0 }`.\
A connection that is not logging returns `not_logging`.

### serial.log.status

Report whether the connection is logging.

```json
{
  "connection_id": "s1a2b3c4"
}
```

Returns `{ "ok": true, "logging": false }`, or, while logging, `logging: true` with `formats`, `files`, `entries`, and\
`dropped_bytes`. `entries` counts logged chunks and events; `dropped_bytes` counts bytes lost because the disk fell\
behind.

### serial.log.mark

Write a labelled marker into the log between the surrounding traffic, for example at the start of a test step.

```json
{
  "connection_id": "s1a2b3c4",
  "label": "step 2: boot"
}
```

The `jsonl` format records `{"event": "mark", "label": "step 2: boot"}` and the `lines` format records the same event.\
The `text` and `raw` files stay byte-identical to the received data. A connection that is not logging returns\
`not_logging`.

---

## Scripts

These tools run, inspect, and cancel validated scripts. The [script reference](scripts.md) explains directives and\
output matching.

### serial.script.run

Run a script on an open connection. The parser checks the whole script before the runner can send anything.

```json
{
  "connection_id": "s1a2b3c4",
  "path": "boot-smoke.script",
  "vars": {
    "SERVER_IP": "192.0.2.1"
  },
  "log": true,
  "log_formats": [
    "text",
    "jsonl"
  ],
  "wait_ms": 1000
}
```

Pass exactly one of `path` or `source`. Paths must identify regular files strictly inside `SERIAL_MCP_SCRIPTS_DIR`,\
which defaults to `.serial_mcp/scripts/`. Inline `source` is limited to 256 KiB. `vars` maps variable names to string\
values. The parser reports every error with its source line. It sends no data if parsing fails.

`log` defaults to `true`. If no port log is active, the run starts one using `log_formats`. Without `log_formats`, it\
uses `SERIAL_MCP_LOG_FORMATS`, then `text`. A log that the run starts is closed when the run ends, so the `log_paths`\
in the result name closed files. A log that was already active stays open. Pass `log: false` to avoid starting a new\
log. `wait_ms` accepts an integer or numeric string from 0 to 60000. Its default is 0, which returns immediately.

The response includes `run_id` and `state`. If the run finishes within `wait_ms`, the response includes the full\
result. Each run also writes a result file under `SERIAL_MCP_LOG_DIR`, which the server never deletes. Parse failures\
return `invalid_params` and an `errors` array. Each entry includes the source line, text, and message. A script `path`\
that exists but cannot be read, or an operating-system error while the parser checks the `@send_file` paths, returns\
`file_unreadable`. A `@send_file` file that exists but cannot be read passes that check, and the run then ends\
`errored` with the reason `file_error`. If the port log cannot be started, the call returns `log_failed`. If the\
connection closes while the script is starting, the call returns `not_found`. A pattern that takes longer than two\
seconds to search ends the run `errored` with the reason `regex_timeout`; see [Limits](scripts.md#limits).

`script_running` blocks `serial.write`, `serial.send_file`, `serial.configure`, `serial.flush`, `serial.set_dtr`,\
`serial.set_rts`, `serial.pulse_dtr`, and `serial.pulse_rts`. `serial.log.start` and `serial.log.stop` also return\
`script_running` while a run owns the connection. Reads, connection status, log marks, and log status remain available.\
The server rejects another run on the claimed connection. Input from a read/write PTY mirror is not blocked; it still\
reaches the device during the run, between the script's writes.

### serial.script.status

Inspect a run by `run_id`, or inspect the latest run on a `connection_id`.

```json
{
  "run_id": "<run id>"
}
```

Status includes the state, current source line and text, completed step count, and elapsed seconds. After completion,\
it also includes the full result, failure details, output tail, result path, and port log paths. An unknown `run_id`,\
or a connection with no retained run, returns `not_found`. The server retains 20 finished results.

### serial.script.cancel

Request cancellation of an active run.

```json
{
  "run_id": "<run id>"
}
```

The handler waits up to five seconds for the runner to stop. If it remains active, the response includes\
`cancel_requested: true`. Cancelling also removes a write of the run that is still queued behind other writes. An\
unknown `run_id` returns `not_found`. The connection stays claimed until the runner returns.

### serial.script.list

List regular files in `SERIAL_MCP_SCRIPTS_DIR`, including each file's byte size and first comment line. If the\
directory does not exist, the tool returns an empty list.

### serial.script.template

Return the directive reference and a script skeleton. The template warns that `@expect` immediately after a command may\
match the device's echo. Anchor the pattern on output the echo cannot contain, or use `@prompt` to consume output\
through a prompt.

---

## Introspection

### serial.connections.list

List all open serial connections with their status, port, configuration, and timestamps. Useful for recovering\
connection IDs after context loss.

```json
{}
```

Returns:

```json
{
  "ok": true,
  "connections": [
    {
      "connection_id": "s1a2b3c4",
      "port": "/dev/ttyUSB0",
      "is_open": true,
      "baudrate": 115200,
      "encoding": "utf-8",
      "opened_at": 1700000000.0,
      "last_seen_ts": 1700000050.0,
      "buffered_bytes": 0,
      "mirror": {
        "pty_path": "/dev/ttys004",
        "link": "/tmp/serial-mcp0",
        "mode": "ro"
      }
    }
  ],
  "count": 1
}
```

The `mirror` field is only present on connections where `SERIAL_MCP_MIRROR` is `ro` or `rw`. `buffered_bytes` shows how\
many unread bytes are in the connection's read buffer.

---

## Protocol Specs

Tools for managing serial device protocol specs. Specs are markdown files with YAML front-matter stored in\
`.serial_mcp/specs/`. The spec index, `.serial_mcp/index.json`, records the registered specs. When it exists but cannot\
be read as a JSON object, every tool that reads it returns `index_corrupt` and leaves the file as it is.

### serial.spec.template

Return a markdown template for a new serial protocol spec.

```json
{
  "device_name": "MyDevice"
}
```

Returns\
`{ "ok": true, "template": "---\nkind: serial-protocol\n...", "suggested_path": ".serial_mcp/specs/mydevice.md" }`.

### serial.spec.register

Register a spec file in the index. Validates YAML front-matter (requires `kind: serial-protocol` and `name`). The path\
must be inside the project directory.

```json
{
  "path": ".serial_mcp/specs/mydevice.md"
}
```

Returns `{ "ok": true, "spec_id": "a1b2c3d4e5f67890", "name": "MyDevice Protocol", ... }`.

### serial.spec.list

List all registered specs with their metadata.

```json
{}
```

Returns `{ "ok": true, "specs": [...], "count": 2 }`.

### serial.spec.attach

Attach a registered spec to a connection session (in-memory only). The spec will be available via `serial.spec.get` for\
the duration of the connection.

```json
{
  "connection_id": "s1a2b3c4",
  "spec_id": "a1b2c3d4e5f67890"
}
```

### serial.spec.get

Get the attached spec for a connection (returns `null` if none attached).

```json
{
  "connection_id": "s1a2b3c4"
}
```

### serial.spec.read

Read full spec content, file path, and metadata by spec_id.

```json
{
  "spec_id": "a1b2c3d4e5f67890"
}
```

### serial.spec.search

Full-text search over a spec's content. Returns matching snippets with line numbers and context.

```json
{
  "spec_id": "a1b2c3d4e5f67890",
  "query": "baud rate",
  "k": 10
}
```

---

## Tracing

Tools for inspecting the JSONL trace log. Tracing is enabled by default and records every tool call. The trace strips\
write `data`, and the inline `source` and `vars` of `serial.script.run`, from traced args unless\
`SERIAL_MCP_TRACE_PAYLOADS` is set.

Each tool call records two events, and `connection_id` is copied from the arguments when present:

```jsonl
{"ts":"2025-01-01T00:00:00.000Z","event":"tool_call_start","tool":"serial.read","args":{"connection_id":"s1"},"connection_id":"s1"}
{"ts":"2025-01-01T00:00:00.050Z","event":"tool_call_end","tool":"serial.read","ok":true,"error_code":null,"duration_ms":50,"connection_id":"s1"}
```

### serial.trace.status

Return tracing config and event count.

```json
{}
```

Returns:

```json
{
  "ok": true,
  "enabled": true,
  "event_count": 42,
  "file_path": ".serial_mcp/traces/trace.jsonl",
  "payloads_logged": false,
  "max_payload_bytes": 16384
}
```

### serial.trace.tail

Return last N trace events (default 50).

```json
{
  "n": 20
}
```

Returns `{ "ok": true, "events": [{ "ts": "...", "event": "tool_call_start", "tool": "serial.read", ... }, ...] }`.

---

## Plugins

Tools for managing user plugins. Plugins live in `.serial_mcp/plugins/` and add device-specific tools without modifying\
the core server; they load only when `SERIAL_MCP_PLUGINS` enables them. [Plugins](plugins.md) explains enabling,\
installing, and writing them.

### serial.plugin.template

Return a Python plugin template. Optionally pre-fill with a device name.

```json
{
  "device_name": "MyDevice"
}
```

Returns\
`{ "ok": true, "template": "\"\"\"Plugin for MyDevice...", "suggested_path": ".serial_mcp/plugins/mydevice.py" }`.

### serial.plugin.list

List loaded plugins with their tool names and metadata.

```json
{}
```

Returns:

```json
{
  "ok": true,
  "plugins": [
    {
      "name": "gps",
      "path": "/path/to/.serial_mcp/plugins/gps.py",
      "tools": [
        "gps.get_position"
      ],
      "meta": {
        "description": "NMEA GPS module plugin",
        "device_name_contains": "GPS"
      }
    }
  ],
  "count": 1,
  "plugins_dir": "/path/to/.serial_mcp/plugins",
  "enabled": true,
  "policy": "*"
}
```

The `meta` field is plugin-defined (optional). Common keys: `description`, `device_name_contains`.

### serial.plugin.reload

Hot-reload a plugin by name. Re-imports the module and refreshes tools.

```json
{
  "name": "gps"
}
```

Returns `{ "ok": true, "name": "gps", "tools": ["gps.get_position"], "notified": true }`.

### serial.plugin.load

Load a new plugin from a file or directory path. The path must be inside `.serial_mcp/plugins/`.

```json
{
  "path": ".serial_mcp/plugins/gps.py"
}
```

Returns:

```json
{
  "ok": true,
  "name": "gps",
  "tools": [
    "gps.get_position"
  ],
  "notified": true,
  "hint": "Plugin loaded on the server. The client may need a restart to call the new tools."
}
```
