# Serial Script Reference

A serial script is UTF-8 text containing device input and output checks. The parser validates the whole script before execution. Any syntax error prevents every step from reaching the port.

## Lines

The parser removes each line ending (`\n` or `\r\n`) before classifying a line.

| Line form | Meaning |
| --- | --- |
| Blank or whitespace-only | Ignored. |
| `# text`, with optional leading whitespace | Comment. The first nonempty comment line describes the script. |
| `@@text` at column zero | A bare line whose sent content starts with `@`. |
| `@name args` at column zero | A directive. Most arguments use Portable Operating System Interface (POSIX) shell quoting; see the rest-of-line rules below. |
| Anything else | A bare line. Its whitespace is preserved, and variables are substituted. The runner appends the configured newline and waits for an enabled prompt. |

Only `@` in column zero begins a directive. A line with leading whitespace before `@` is sent as a bare line. A `#` after optional leading whitespace begins a comment. Use `@sendline # text` to send text starting with `#`.

## Directives

| Directive | Syntax | Effect |
| --- | --- | --- |
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

`@send`, `@sendline`, `@sendhex`, `@newline`, and `@mark` take the rest of their line without quote processing. Quotes in those lines remain literal. Other directives use POSIX shell-style argument quoting, where quotes group words. The parser substitutes variables after splitting, so spaces and quotes in values stay inside one argument.

The text directives `@send`, `@sendline`, and `@newline` decode escapes before variable substitution. Backslashes in variable values therefore stay literal. `@sendhex` sends raw bytes. Text writes use the connection's encoding. In those directives, `\xHH` is a Unicode code point encoded with that setting.

`@send_file` paths must resolve to regular files inside the configured files directory. Directories, other file types, paths outside that directory, and files larger than 1 MiB are refused. Quote paths that contain spaces, as in `@send_file "images/boot image.bin"`.

## Durations and pacing

A duration is a number followed by `ms`, `s`, or `m`, such as `250ms`, `10s`, or `1.5m`. A bare number is an error because its unit is ambiguous.

Pacing delays must be whole numbers of milliseconds from 1 to 1000. The chunk size `N` must be a positive whole number.

`@pacing`, `@newline`, `@timeout`, `@prompt`, and `@fail_on` affect the current run only. They do not change the connection's configuration.

## Variables

Write variables as `{{NAME}}`. A name starts with a letter or underscore, followed by letters, digits, or underscores. An undefined variable is a parse error with its source line. The parser leaves `${...}` unchanged for shells and bootloaders that use that form.

Inside a regular expression, variable values are escaped so they match literal text rather than regular-expression syntax. For example, `@expect {{VERSION}}` with `VERSION=U-Boot 2026.10` matches that text literally.
Patterns use Python regular-expression syntax. Inline flags, such as `(?i)`, work.

Each step retains its original source line before substitution. Result step text retains `{{SERVER_IP}}`. The variable-name list contains only `SERVER_IP`, never its value. Device output is recorded as received, so echoes can still expose values in output and port logs.

## Matching device output

A matcher stores decoded output that arrives after a run subscribes to the reader. It decodes bytes incrementally with the connection's encoding and replaces invalid sequences.

A cursor marks the start of unconsumed text. `@expect` and prompt waits search after the cursor, then move it through the match. One occurrence cannot satisfy two waits.

Fail patterns search new output whenever the runner wakes. They also include the 256 characters before the latest output, so a pattern split across chunks still matches. Pattern evaluation runs on the runner thread, not the reader thread.

The matcher holds at most 1 MiB of unconsumed text. It drops older text beyond that limit. The result records `window_overflow` and the exact drop count.

Devices often echo input. For example, `@expect ls` after the bare command `ls` can match the echo instead of the command's result. Anchor `@expect` on output that the echo cannot contain. Alternatively, enable a prompt so the runner consumes each command through its prompt.

## Errors

The parser returns every syntax error together, with its source line. Unknown directives, malformed arguments, invalid regular expressions, undefined variables, unitless durations, and rejected `@send_file` paths produce errors. Fix them before starting a run.

The script language has no shell escape or include directive. `@send_file` is its only file operation, and it is confined to the configured files directory.
