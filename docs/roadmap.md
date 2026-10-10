# Roadmap

This file lists the known work that no plan covers yet: deferred features, accepted limits worth revisiting, and gaps\
in the tests. Each item says what is missing and why it matters. An item leaves this file when a design or plan takes\
it up, or when it lands and the CHANGELOG records it. Add new follow-ups here rather than in a plan's execution record,\
so they stay in one place.

The items below were collected from the archived\
[pacing, logging, and script plan](plans/archive/pacing-logging-scripts-implementation-plan.md) and\
[script runner design](plans/archive/script-runner-design.md) on 10 October 2026. Items without a source note came from\
those two documents.

## Features That Need a Design

### Log and result rotation

The server never deletes port logs or script result files, and a log never rotates, so a long-lived deployment fills\
its log directory; operators clean it by hand today. A design should set size caps per file and per directory and prune\
old files. It should also decide whether an operator can make the environment authoritative, because an agent can\
enable logging and choose formats on its own, and `jsonl` is about four times the size of the raw bytes. The\
collision-free names stop after 1,000 files with the same timestamp prefix.

### A cleanup block for scripts

A failed run leaves the device where the failure happened. A `@finally` section would run cleanup steps on every exit;\
until then, a CI job can run a separate reset script.

### Mirror input in script results

Operator input through a read/write mirror reaches the device mid-run and can change the outcome. The port log records\
it with source `mirror`, but the result does not flag it.

### Limits on a write already in flight under a script

The stop event ends such a write between segments, but `SERIAL_MCP_SCRIPT_MAX_SECONDS` and fail patterns are checked\
only when it returns, and a script's writes skip `SERIAL_MCP_MAX_WRITE_SECONDS`. A long paced `@send_file` can\
therefore outlast the run's maximum duration.

### Encoding checks at parse time

Text that the connection's encoding cannot encode is found only when its step runs, because the parser takes no\
encoding.

## Security and Operations

### Port log memory bound

The queue limit counts payload bytes only, so a stalled disk lets many small entries and events use more memory than\
the 8 MiB limit.

### Default data directory

The default log directory, `<spec root>/logs/`, can sit inside another project's tree, where `.gitignore` does not\
cover it. A per-user state directory would avoid that.

### Directory modes

Creating a log directory sets mode `0700` only on the last directory it creates and never tightens an existing one.\
File contents stay private, because files are created `0600`.

### Spec registration reads any project file

`serial.spec.register` reads any file under the project root that has valid front matter and returns its content. If\
the agent should read nothing outside the data directories, this channel needs its own review.

## Platform Support

### Windows

The suite has never run on Windows, and CI does not test it. The `serial.send_file` confinement checks rely on stable\
inodes and `/proc`, so they are weaker there and untested.

## Test Coverage

### Path confinement layers

On Linux, the later `/proc` check and the check on the open handle repeat the earlier refusals with the same message,\
so removing one protection does not fail a test. Tests should isolate each layer.

### Wall-clock bounds

No test bounds the shutdown time (0.25 s), the `serial.send_file` delay, the file-mode check under a restrictive umask,\
or the runner's one-wake polling.

### Behavior pinned only indirectly

The final conformance review of the script work listed behavior that no test pins directly: the five-second held-cancel\
wait and claim release, the plugin path through `handle_write`, consumption at runner level, `window_overflow` in the\
result, the end-of-stream branch inside a wait, `O_NOFOLLOW` and `O_EXCL` in `open_exclusive` (masked by the\
symbolic-link pre-check), a strictly increasing `t` in the log, the 500 ms idle default, the top-level fields of\
`serial.script.status`, repeated `--var`, exit status 2 for a run that ends cancelled without an interrupt, a flush\
that cancels a running paced job, `serial.close` during a paced write at handler level, the next mirror forward after\
`serial.configure`, the forward-failure warning, and signalling every thread before joining.

## From the Package Layout Work

These came from the [package layout plan](plans/package-layout-implementation-plan.md), finished on 9 October 2026.

### A bounded join in the script-run cleanup

When starting a run's watcher thread fails, the cleanup in `serial.script.run` waits for the runner without a limit.\
Bounding it needs a decision on releasing the connection's claim while the runner may still be running.

### Write jobs below the MCP layer

`run_write_job` and the write-duration check live in `mcp/`. A non-MCP caller, such as the command-line runner, would\
need them moved down; nothing needs that today.

## From the Code Comment Review

These came from documenting the hardcoded limits on 10 October 2026.

### A cancelled DTR or RTS pulse leaves the line low

`serial.pulse_dtr` and `serial.pulse_rts` set the line low, sleep, and set it high. A request cancelled during the\
sleep skips the last step, so the line stays low, which can hold a device in reset. Restoring the line in a `finally`\
fixes it.

### A zero read timeout may busy-loop the reader

With `timeout_ms` 0 on `serial.open`, the reader's one-byte read returns at once when nothing is waiting, so the reader\
thread may spin. Not yet reproduced; either reject 0 or make the reader wait when the read returns nothing.

### Trace edge cases

`TraceBuffer.tail(0)` returns every event, because `items[-0:]` is the whole list, and a negative count also returns an\
odd slice. A write error in the trace file sink fails the tool call that emitted the event, instead of only disabling\
or reporting the sink.

### Duplicated constants

The `@fail_on` overlap of 256 characters is defined in both `script/runner.py` and `script/matcher.py`, and the default\
trace payload limit of 16384 in both `mcp/trace.py` and `config.py`. Each pair must stay equal by hand; the runner and\
the trace module should import the value instead.

### Shutdown limit and worker threads

`SHUTDOWN_LIMIT_SECONDS` (0.25 s) in `mcp/server.py` abandons `stop_connections` when it runs long, but AnyIO worker\
threads are not daemon threads, so the interpreter may still wait for it at exit. Not verified end to end.
