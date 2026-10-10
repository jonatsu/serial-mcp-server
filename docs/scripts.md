# Serial Script Reference

A serial script is UTF-8 text containing device input and output checks. The parser validates the whole script before\
execution. Any syntax error prevents every step from reaching the port.

## Lines

The parser removes each line ending (`\n` or `\r\n`) before classifying a line.

| Line form | Meaning |
| -- | -- |
| Blank or whitespace-only | Ignored. |
| `# text`, with optional leading whitespace | Comment. The first nonempty comment line describes the script. |
| `@@text` at column zero | A bare line whose sent content starts with `@`. |
| `@name args` at column zero | A directive. Most arguments use Portable Operating System Interface (POSIX) shell quoting; see the rest-of-line rules below. |
| Anything else | A bare line. Its whitespace is preserved, and variables are substituted. The runner appends the configured newline and waits for an enabled prompt. |

Only `@` in column zero begins a directive. A line with leading whitespace before `@` is sent as a bare line. A `#`\
after optional leading whitespace begins a comment. Use `@sendline # text` to send text starting with `#`.

## Directives

| Directive | Syntax | Effect |
| -- | -- | -- |
| `@send` | `@send TEXT` | Send the remaining text without a newline. Decode `\r`, `\n`, `\t`, `\xHH`, and `\\`. |
| `@sendline` | `@sendline TEXT` | Send the remaining text, append the configured newline, and wait for an enabled prompt. |
| `@sendhex` | `@sendhex HEX` | Send raw bytes decoded from hexadecimal digits. Spaces are allowed. |
| `@expect` | `@expect REGEX [--timeout D]` | Wait for a regular expression after the cursor. Consume through the match; `--timeout` overrides this step's timeout. |
| `@prompt` | `@prompt REGEX` or `@prompt --off` | Set the prompt consumed after bare lines and `@sendline`, or disable prompt waits. Prompt waiting starts disabled. |
| `@fail_on` | `@fail_on REGEX` or `@fail_on --clear` | Add a failure pattern; several can stay active. Clear all active patterns with `--clear`. |
| `@timeout` | `@timeout D` | Set the default timeout for expects and prompt waits. It starts at `10s`. |
| `@sleep` | `@sleep D` | Wait for a duration while checking active failure patterns. |
| `@discard` | `@discard` | Discard all output currently held by the matcher. |
| `@dtr` | `@dtr on` or `@dtr off` | Set the data terminal ready (DTR) control line. |
| `@rts` | `@rts on` or `@rts off` | Set the request to send (RTS) control line. |
| `@pulse` | `@pulse dtr D` or `@pulse rts D` | Set the selected control line low for a duration, then high. |
| `@pacing` | `@pacing off`, `@pacing char D`, or `@pacing chunk N D` | Override pacing on later writes. Otherwise, connection pacing applies. |
| `@newline` | `@newline TEXT` | Set the newline appended to later bare lines and `@sendline`. Initially, it uses the connection's newline. |
| `@mark` | `@mark LABEL` | Write the remaining text as a port-log mark and record it in the result. |
| `@send_file` | `@send_file PATH` | Send a confined regular file from `SERIAL_MCP_FILES_DIR` with current pacing. |

`@send`, `@sendline`, `@sendhex`, `@newline`, and `@mark` take the rest of their line without quote processing. Quotes\
in those lines remain literal. The parser strips leading and trailing whitespace from that text, so write `\x20` or\
`\t` for a space or tab at either end of `@send`, `@sendline`, or `@newline` text. Other directives use POSIX\
shell-style argument quoting, where quotes group words. The parser substitutes variables after splitting, so spaces and\
quotes in values stay inside one argument.

The text directives `@send`, `@sendline`, and `@newline` decode escapes before variable substitution. Backslashes in\
variable values therefore stay literal. An escape other than `\r`, `\n`, `\t`, `\xHH`, and `\\` is a parse error.\
`@sendhex` sends raw bytes. Text writes use the connection's encoding. In those directives, `\xHH` is a Unicode code\
point encoded with that setting.

`@send_file` paths must resolve to regular files inside the configured files directory. Directories, other file types,\
paths outside that directory, and files larger than 1 MiB are refused. Quote paths that contain spaces, as in\
`@send_file "images/boot image.bin"`.

## Durations and pacing

A duration is a number followed by `ms`, `s`, or `m`, such as `250ms`, `10s`, or `1.5m`. A bare number is an error\
because its unit is ambiguous.

Pacing delays must be whole numbers of milliseconds from 1 to 1000. The chunk size `N` must be a positive whole number.

`@pacing`, `@newline`, `@timeout`, `@prompt`, and `@fail_on` affect the current run only. They do not change the\
connection's configuration.

## Variables

Write variables as `{{NAME}}`. A name starts with a letter or underscore, followed by letters, digits, or underscores.\
An undefined variable is a parse error with its source line. The parser leaves `${...}` unchanged for shells and\
bootloaders that use that form.

Inside a regular expression, variable values are escaped so they match literal text rather than regular-expression\
syntax. For example, `@expect {{VERSION}}` with `VERSION=U-Boot 2026.10` matches that text literally. Patterns use the\
syntax of the [`regex`](https://pypi.org/project/regex/) package, which accepts Python `re` syntax and adds features\
such as Unicode property classes (`\p{N}`). Inline flags, such as `(?i)`, work.

Each step retains its original source line before substitution. Result step text retains `{{SERVER_IP}}`. The\
variable-name list contains only `SERVER_IP`, never its value. Values still appear elsewhere: device output is recorded\
as received, so echoes can expose values in output and port logs; the `tx` entries of the port log hold every byte\
sent, including substituted values; and a variable used in an `@mark` label is stored in the result's `marks` and in\
the port log.

## Matching device output

A matcher stores decoded output that arrives after a run subscribes to the reader. It decodes bytes incrementally with\
the connection's encoding and replaces invalid sequences.

A cursor marks the start of unconsumed text. `@expect` and prompt waits search after the cursor, then move it through\
the match. One occurrence cannot satisfy two waits.

Fail patterns search new output whenever the runner wakes. They also include the 256 characters before the latest\
output, so a pattern split across chunks still matches. The runner thread asks for these searches, never the reader\
thread, and every search has a time limit (see [Limits](#limits)).

The matcher holds at most 1 MiB of unconsumed text. It drops older text beyond that limit. The result records\
`window_overflow` and the exact drop count.

A pattern that can match the empty string, such as `@expect "x*"`, matches immediately at the cursor and consumes\
nothing. Anchor expects on output the echo cannot produce, or require at least one character.

Devices often echo input. For example, `@expect ls` after the bare command `ls` can match the echo instead of the\
command's result. Anchor `@expect` on output that the echo cannot contain. Alternatively, enable a prompt so the runner\
consumes each command through its prompt.

## Errors

The parser returns every syntax error together, with its source line. Unknown directives, malformed arguments, invalid\
regular expressions, undefined variables, unitless durations, and rejected `@send_file` paths produce errors. Fix them\
before starting a run.

The script language has no shell escape or include directive. `@send_file` is its only file operation, and it is\
confined to the configured files directory.

## Limits

Each pattern search has a limit, two seconds unless `SERIAL_MCP_SCRIPT_PATTERN_SECONDS` sets another. A pattern that\
backtracks without end, such as `(a|aa)+$` against a long run of `a` characters, stops at the limit and ends the run\
`errored` with the reason `regex_timeout`. The search releases the interpreter lock, so the server keeps answering\
while it runs. A cancel that arrives during a search takes effect when the search returns, at the latest at the limit.\
If a pattern hits the limit, rewrite it without nested or overlapping quantifiers.

`SERIAL_MCP_SCRIPT_MAX_SECONDS` and `@fail_on` are checked between steps and when output arrives, not while a single\
write is in flight. A long paced `@send` or `@send_file` therefore runs to its end unless the run is cancelled, and a\
`@fail_on` pattern that appears during the write is noticed only afterwards. A script's writes are not subject to\
`SERIAL_MCP_MAX_WRITE_SECONDS`.

Text that the connection's encoding cannot encode is found when its step runs, after earlier steps were sent. The run\
then ends `errored` with the reason `serial_error`.

## Running from the command line

`serial_mcp_script` runs one script on one serial port without an agent or the MCP server. It uses the same engine,\
writer, reader, and port log as the MCP tools.

```bash
serial_mcp_script SCRIPT --port PORT [options]
```

| Option | Meaning |
| -- | -- |
| `SCRIPT` | Path of the script. Any readable path works. |
| `--port PORT` | Serial port to open. Required. |
| `--baud N` | Baud rate. Default `115200`. |
| `--var NAME=VALUE` | Set a script variable. Repeat for several. The value starts after the first `=`. The command exits with status `2` if a name breaks the [variable naming rule](#variables). |
| `--log-format FORMAT` | Port log format: `text`, `jsonl`, `lines`, or `raw`. Repeat for several. Default: `SERIAL_MCP_LOG_FORMATS`, else `text`. |
| `--no-log` | Do not record a port log. |
| `--log-dir DIR` | Directory for the port log and, unless `--result` is set, the result file. Default: `SERIAL_MCP_LOG_DIR`, else `.serial_mcp/logs/`. |
| `--files-dir DIR` | Directory `@send_file` reads from. Default: `SERIAL_MCP_FILES_DIR`, else `.serial_mcp/files/`. |
| `--result FILE` | Write the result file here. |

The port opens with 8 data bits, no parity, one stop bit, 200 ms read and write timeouts, UTF-8 text, the newline\
`\r\n`, and an exclusive lock. The command records a port log unless you pass `--no-log`.

The command reads and parses the script before it opens the port. A parse error prints every error with its line and\
exits with status `2`; nothing reaches the port. If `--result` names a file that already exists, the command exits with\
status `2` before it opens the port, so a result is never overwritten. A missing port exits with status `2` and a\
message, and so does a port that another program holds with an exclusive lock. The lock stops only programs that ask\
for one too, so close the port in the MCP server and in other tools before you run a script.

While the script runs, the command prints the line number and text of the current step to standard error. It checks\
five times a second, so a step that ends sooner may not appear; the result file lists every step. When the run ends,\
the command prints a summary: the final state, the reason, the failing line and its text, the device output before the\
failure, the result file path, and the log paths. Standard output stays empty.

### Exit status

| Status | Meaning |
| -- | -- |
| `0` | The script passed. |
| `1` | The script failed: an `@expect` timed out, an `@fail_on` pattern matched, or another check failed. |
| `2` | The run errored, the arguments or script were unusable, the port could not be opened, the run ended `cancelled` without an interrupt, or the command hit an unexpected error. |
| `130` | You interrupted the command with Ctrl-C. |

### Result file

The result file has the same structure as the one the MCP tools write: `schema_version`, `state`, `steps`, `failure`,\
`log_paths`, and the variable names, never their values. Without `--result`, the file goes in the log directory with a\
name that cannot collide with an earlier run. With `--result`, the command creates the file's parent directories and\
refuses an existing file.

### Interrupting

Ctrl-C (SIGINT) cancels the run. The command waits up to five seconds for the run to stop, then stops the writer and\
reader, closes the port log and the port, and exits with status `130`. The result file records the state `cancelled`. A\
second Ctrl-C does not skip that cleanup. Other signals, such as SIGTERM, end the command at once and write no result\
file.

### Paths

The script path is not confined: the command reads whatever file you name. `@send_file` stays confined to the files\
directory, as it is under the MCP tools.
