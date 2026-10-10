#!/usr/bin/env python3
"""End-to-end harness for pacing, port logging, and scripts.

Run from the repository root:  just integration  (or: uv run python tests/integration/run.py)

Real processes only: examples/demo-device/pty_server.py, the MCP server over stdio, the serial_mcp_script CLI, and
real pseudo-terminals. It needs Linux, because it reads /proc to find its processes. Temporary files live under /tmp
and are removed at the end (``--keep-work`` keeps them).
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread

from serial_mcp_server.config import DEFAULT_SCRIPT_PATTERN_SECONDS as PATTERN_TIMEOUT_SECONDS

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (
    ROOT,
    TOKEN,
    TOKEN_ENV,
    Check,
    Device,
    Fleet,
    LogFiles,
    Probe,
    Report,
    Terminal,
    align_lines,
    find_server_pid,
    load_log,
    mcp_server,
    ms_stats,
    percentile,
    stray_processes,
)

CHAR_DELAY_S = 0.008
LINE = b"ping  \r\n"  # 8 bytes; the demo device strips it to "ping" and answers
REPLY = b"pong\r\n> "  # 8 bytes
BASELINE_LINES = 25
PASTE_LINES = 512  # 512 * 8 bytes = 4096 bytes = 4 KiB
PASTE = LINE * PASTE_LINES
RUN_DEADLINE_SECONDS = 900  # the whole harness, so a hung scenario cannot hold up the run

PASS_SCRIPT = r"""# Demo device: version and ping, with explicit expects and no automatic prompt waits
@prompt --off
@timeout 3s
@sendline version
@expect "DemoDevice v1\.0\.0"
@expect "> "
@sendline ping
@expect pong
@expect "> "
"""
WRONG_SCRIPT = PASS_SCRIPT.replace(r"DemoDevice v1\.0\.0", r"DemoDevice v9\.9\.9")
WRONG_LINE = 5
# The pattern must never match: a pattern that can match, such as (a|aa)+$, matches the a's of a partial read
# before the b arrives, and the run passes instead of reaching the limit.
CATASTROPHIC_PATTERN = "(a|aa)+c"
CATASTROPHIC_SCRIPT = (
    "# The demo device echoes forty a's, and (a|aa)+c backtracks without end on them.\n"
    "@prompt --off\n"
    "@timeout 60s\n"
    f"@sendline echo {'a' * 40}b\n"
    f'@expect "{CATASTROPHIC_PATTERN}"\n'
)
CATASTROPHIC_LINE = 5
RESPONSE_LIMIT_SECONDS = 1.0  # the longest any status call may take while a pattern runs away
STALL_CONTROL_SECONDS = 1.5

SERVER_LOG_FORMATS = "jsonl,text,lines,raw"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def server_env(work: Path) -> dict[str, str]:
    return {
        "SERIAL_MCP_MIRROR": "rw",
        "SERIAL_MCP_LOG": "1",
        "SERIAL_MCP_LOG_FORMATS": SERVER_LOG_FORMATS,
        "SERIAL_MCP_LOG_DIR": str(work / "logs"),
        "SERIAL_MCP_FILES_DIR": str(work / "files"),
        "SERIAL_MCP_SCRIPTS_DIR": str(work / "scripts"),
        "SERIAL_MCP_MIRROR_LINK": str(work / "mirror-link"),
    }


async def open_device(srv: Any, device: Device, **extra: Any) -> dict[str, Any]:
    """Open the device through the server. The boot banner was already consumed by the harness (see Device)."""
    return await srv.call_ok("serial.open", port=device.path, **extra)


async def frozen_line(terminal: Terminal, probe: Probe, replies_before: int) -> float:
    """Sensitivity control: SIGSTOP the server for 400 ms just before a line's last byte, return the measured latency.

    Shows that the latency measurement used for bullet 1 can see a delay when one exists.
    """
    pid = find_server_pid()
    head = terminal.paste(LINE[:-1], CHAR_DELAY_S)
    await head.wait(5)
    os.kill(pid, signal.SIGSTOP)
    try:
        tail = terminal.paste(LINE[-1:], 0.0)
        await tail.wait(5)
        await anyio.sleep(0.4)
    finally:
        os.kill(pid, signal.SIGCONT)
    arrival = await probe.wait_total(len(REPLY) * (replies_before + 1), 5)
    if arrival is None:
        raise RuntimeError("no reply after the frozen server was continued")
    return arrival - tail.line_done[0]


# --------------------------------------------------------------------------- bullets 1 and 2


async def _paste_scenario(
    terminal: Terminal, probe: Probe, burst: bool, check: Check, measurements: dict[str, Any]
) -> None:
    """The typed baseline, the frozen-line control, and the paste, while the probe reads; fills *measurements*."""
    baseline: list[float] = []
    typed = b""
    if not burst:
        for n in range(1, BASELINE_LINES + 1):
            line = terminal.paste(LINE, CHAR_DELAY_S)
            await line.wait(5)
            typed += LINE
            arrival = await probe.wait_total(len(REPLY) * n, 3)
            if arrival is None:
                check.error(f"baseline line {n}: no reply within 3 s")
                break
            baseline.append(arrival - line.line_done[0])
            await anyio.sleep(0.1)
        measurements["frozen_latency"] = await frozen_line(terminal, probe, len(baseline))
        typed += LINE
    lines_before_paste = len(baseline) + (1 if not burst else 0)
    paste = terminal.paste(PASTE, 0.0 if burst else CHAR_DELAY_S)
    typed += PASTE
    first_line = lines_before_paste
    await paste.wait(120)
    handed_over = paste.finished_at - paste.started_at
    total = len(REPLY) * (first_line + PASTE_LINES)
    last_arrival = await probe.wait_total(total, 90 if burst else 10)
    paste_end = paste.finished_at
    await anyio.sleep(0.7)  # settle: nothing more should arrive
    extra = len(probe.data) - total
    measurements.update(
        baseline=baseline,
        handed_over=handed_over,
        paste=paste,
        first_line=first_line,
        last_arrival=last_arrival,
        paste_end=paste_end,
        extra=extra,
        typed=typed,
    )


async def run_paste(
    srv: Any, device: Device, *, burst: bool, check: Check
) -> tuple[LogFiles, bytes, dict[str, Any]]:
    """Paste 4 KiB of complete ``ping`` lines into the mirror terminal while polling serial.read.

    ``burst=False``: the terminal types one byte per 8 ms (a 4 KiB paste takes ~32.8 s).
    ``burst=True``: the terminal pastes everything at once and the connection's ``inter_char_delay_ms=8`` paces the wire.
    Returns the parsed log, the bytes the probe received, and measurements.
    """
    opened = await open_device(srv, device, inter_char_delay_ms=8)
    cid = opened["connection_id"]
    mirror = opened["mirror"]
    check.expect(mirror["mode"] == "rw" and bool(mirror["pty_path"]), f"serial.open reports mirror {mirror}")
    check.expect(opened["config"]["inter_char_delay_ms"] == 8, "connection pacing is 8 ms per character")
    check.note(f"log files at open: {sorted(opened['log']['files'])}")
    terminal = Terminal(mirror["pty_path"])
    probe = Probe(srv, cid)
    measurements: dict[str, Any] = {}
    async with anyio.create_task_group() as probe_group:
        probe.start(probe_group)
        try:
            await _paste_scenario(terminal, probe, burst, check, measurements)
        finally:
            await probe.stop()
    measurements["probe"] = probe
    measurements["terminal_received"] = len(terminal.received)
    terminal.close()
    stop = await srv.call_ok("serial.log.stop", connection_id=cid)
    await srv.call_ok("serial.close", connection_id=cid)
    return load_log(stop), bytes(probe.data), measurements


def analyze_terminal_paste(check: Check, m: dict[str, Any]) -> None:
    probe: Probe = m["probe"]
    paste = m["paste"]
    baseline: list[float] = m["baseline"]
    first_line: int = m["first_line"]
    last_arrival = m["last_arrival"]
    latencies: list[float] = []
    for n in range(PASTE_LINES):
        arrival = probe.time_reaching(len(REPLY) * (first_line + n + 1))
        if arrival is None:
            check.error(f"paste line {n + 1}: reply never arrived")
            return
        latencies.append(arrival - paste.line_done[n])
    answered_during = sum(
        1
        for n in range(PASTE_LINES)
        if probe.time_reaching(len(REPLY) * (first_line + n + 1)) < m["paste_end"]
    )  # type: ignore[operator]
    duration = m["paste_end"] - paste.started_at
    check.note(
        f"paste: {len(PASTE)} bytes, {PASTE_LINES} lines of 'ping', 8 ms per byte; took {duration:.2f} s (ideal {len(PASTE) * CHAR_DELAY_S:.2f} s)"
    )
    check.note(
        f"serial.read calls during the whole run: {probe.calls}; non-empty returns: {len(probe.times)}"
    )
    check.note(f"baseline latency (one line at a time, no paste in flight) {ms_stats(baseline)}")
    check.note(
        f"paste latency (line completed in terminal -> its reply read via serial.read) {ms_stats(latencies)}"
    )
    check.note(f"replies already read before the paste ended: {answered_during} of {PASTE_LINES}")
    lag = (last_arrival - m["paste_end"]) if last_arrival is not None else None
    check.note(
        f"last reply read {lag * 1000:.1f} ms after the paste ended"
        if lag is not None
        else "last reply never arrived"
    )
    check.expect(
        duration >= len(PASTE) * CHAR_DELAY_S - 0.05, "the paste really took the paced time (not a burst)"
    )
    frozen = m["frozen_latency"]
    check.note(
        f"control: server frozen with SIGSTOP for 400 ms before one line's last byte -> measured latency {frozen * 1000:.1f} ms"
    )
    check.expect(
        0.35 <= frozen <= 1.5,
        "control: the measurement sees an induced 400 ms stall (it would flag a real one)",
    )
    check.expect(
        percentile(latencies, 0.95) <= percentile(baseline, 0.95) + 0.050,
        f"paste p95 latency {percentile(latencies, 0.95) * 1000:.1f} ms <= baseline p95 {percentile(baseline, 0.95) * 1000:.1f} ms + 50 ms",
    )
    check.expect(max(latencies) <= 0.250, f"paste worst latency {max(latencies) * 1000:.1f} ms <= 250 ms")
    check.expect(
        answered_during >= PASTE_LINES - 2,
        f"output was read during the paste ({answered_during}/{PASTE_LINES} lines before it ended)",
    )
    check.expect(
        lag is not None and lag <= 2.0, "the full expected output arrived within 2 s after the paste ended"
    )
    check.expect(m["extra"] == 0, f"no unexpected extra bytes after the output (extra={m['extra']})")


def analyze_burst_paste(check: Check, m: dict[str, Any], log: LogFiles) -> None:
    probe: Probe = m["probe"]
    tx = [r for r in log.jsonl if r.get("dir") == "tx"]
    # The log's `t` is seconds on the server's monotonic clock; the probe uses this process's monotonic clock.
    # Both run at the same rate, so differences between lines are comparable without knowing the clocks' offset.
    newline_t = [r["t"] for r in tx for byte in bytes.fromhex(r["hex"]) if byte == 0x0A]
    wire_start, wire_end = tx[0]["t"], tx[-1]["t"]
    sizes = sorted({len(bytes.fromhex(r["hex"])) for r in tx})
    check.note(f"terminal handed 4096 bytes to the PTY in {m['handed_over'] * 1000:.1f} ms")
    check.note(
        f"tx log: {len(tx)} entries, entry sizes {sizes}, wire time first->last tx {wire_end - wire_start:.2f} s (ideal {len(PASTE) * CHAR_DELAY_S:.2f} s)"
    )
    arrivals = [probe.time_reaching(len(REPLY) * (n + 1)) for n in range(PASTE_LINES)]
    if any(a is None for a in arrivals) or len(newline_t) != PASTE_LINES:
        check.error(
            f"incomplete: {sum(a is None for a in arrivals)} replies missing, {len(newline_t)} tx line terminators logged"
        )
        return
    # How much later than line 1 each reply was read, compared with when its line left the port.
    lag = [(a - arrivals[0]) - (s - newline_t[0]) for a, s in zip(arrivals, newline_t, strict=True)]  # type: ignore[operator]
    tx_span = newline_t[-1] - newline_t[0]
    read_span = arrivals[-1] - arrivals[0]  # type: ignore[operator]
    check.note(
        f"line 1 -> line {PASTE_LINES}: left the port over {tx_span:.2f} s; replies read over {read_span:.2f} s"
    )
    check.note(f"reply read time minus line wire time, relative to line 1 (so ~ added delay) {ms_stats(lag)}")
    check.note(f"serial.read calls {probe.calls}, non-empty returns {len(probe.times)}")
    check.expect(
        len(newline_t) == PASTE_LINES,
        f"the tx log holds all {PASTE_LINES} line terminators ({len(newline_t)})",
    )
    check.expect(m["handed_over"] < 2.0, "the pasting terminal was not stalled (4 KiB accepted within 2 s)")
    check.expect(
        wire_end - wire_start >= len(PASTE) * CHAR_DELAY_S * 0.9,
        "the wire time shows 8 ms pacing was applied to the paste",
    )
    check.expect(
        abs(read_span - tx_span) <= 0.25,
        "replies were read progressively at the paste's own pace (read span within 250 ms of wire span)",
    )
    check.expect(
        percentile(lag, 0.95) <= 0.100, f"p95 added delay {percentile(lag, 0.95) * 1000:.1f} ms <= 100 ms"
    )
    check.expect(max(lag) <= 0.250, f"worst added delay {max(lag) * 1000:.1f} ms <= 250 ms")
    check.expect(m["extra"] == 0, f"no unexpected extra bytes after the output (extra={m['extra']})")
    check.expect(m["last_arrival"] is not None, "the whole expected output (4096 bytes) arrived")


def analyze_log_agreement(check: Check, log: LogFiles, oracle: bytes, typed: bytes) -> None:
    rx = log.stream("rx")
    tx = log.stream("tx")
    check.note(
        f"bytes: serial.read oracle={len(oracle)} sha={sha(oracle)} | raw={len(log.raw)} sha={sha(log.raw)} | text={len(log.text)} sha={sha(log.text)} | jsonl rx={len(rx)} sha={sha(rx)}"
    )
    check.expect(log.raw == oracle, "raw file == every byte serial.read returned")
    check.expect(log.text == log.raw, "text file == raw file (no timestamps, rx only)")
    check.expect(rx == log.raw, "concatenated jsonl rx hex == raw file")
    rx_entries = [e for e in log.lines if e["dir"] == "rx"]
    tx_entries = [e for e in log.lines if e["dir"] == "tx"]
    partial = sum(1 for e in rx_entries if e.get("partial"))
    newline_count = log.raw.count(b"\n")
    check.note(
        f"lines file: {len(rx_entries)} rx entries ({partial} partial), {len(tx_entries)} tx entries; raw holds {newline_count} newlines"
    )
    problems = align_lines(rx_entries, rx, log.spans("rx"))
    check.expect(
        not problems,
        f"every rx line entry covers exactly its bytes of the stream, in order, with the ts/t of the jsonl chunk holding its first byte {problems[:2]}",
    )
    corrupted = bytearray(rx)
    corrupted[len(corrupted) // 2] ^= 0x01
    check.expect(
        bool(align_lines(rx_entries, bytes(corrupted), log.spans("rx"))),
        "control: a one-byte corruption of the stream is detected by the same comparison",
    )
    check.expect(
        bool(align_lines(rx_entries[:100] + rx_entries[101:], rx, log.spans("rx"))),
        "control: a dropped line entry is detected by the same comparison",
    )
    check.expect(
        sum(1 for e in rx_entries if not e.get("partial")) == log.raw.count(b"\n"),
        "number of complete rx lines == number of newlines in the raw file",
    )
    check.expect(
        log.raw.split(b"\n")[-1] == b"> "
        and rx_entries[-1]["text"] == "> "
        and bool(rx_entries[-1].get("partial")),
        "the trailing prompt '> ' (no newline) is kept as a partial last line",
    )
    check.expect(tx == typed, f"jsonl tx bytes == bytes typed into the terminal ({len(tx)} vs {len(typed)})")
    check.expect(
        all(r.get("source") == "mirror" for r in log.jsonl if r.get("dir") == "tx"),
        "every tx entry is attributed to source 'mirror'",
    )
    problems = align_lines(tx_entries, tx, log.spans("tx"))
    check.expect(not problems, f"every tx line entry matches its bytes and stamp {problems[:2]}")
    check.expect(
        not log.events("log_overflow") and not log.events("buffer_drop"),
        "no log_overflow or buffer_drop events",
    )
    check.note(f"jsonl events: {sorted({r['event'] for r in log.jsonl if 'event' in r})}")


# --------------------------------------------------------------------------- bullet 3


async def check_send_file(srv: Any, device: Device, work: Path) -> Check:
    check = Check(
        "3", "serial.send_file with chunk pacing appears as paced tx segments between start and end events"
    )
    payload = "".join(f"echo L{i:02d}-abcdef\n" for i in range(10)).encode()
    assert len(payload) == 160
    files = work / "files" / "env"
    files.mkdir(parents=True)
    (files / "boot.txt").write_bytes(payload)
    opened = await open_device(srv, device)
    cid = opened["connection_id"]
    paced = await srv.call_ok(
        "serial.send_file", connection_id=cid, path="env/boot.txt", chunk_size=12, chunk_delay_ms=20
    )
    check.expect(
        paced["bytes_written"] == 160,
        f"paced send_file wrote {paced['bytes_written']} bytes, path {paced['path']!r}",
    )
    reply = await srv.call_ok(
        "serial.read_until", connection_id=cid, delimiter="L09-abcdef\r\n> ", timeout_ms=5000, max_bytes=4096
    )
    check.expect(
        reply["data"].endswith("L09-abcdef\r\n> "), "the device acted on the file: its last reply came back"
    )
    control = await srv.call_ok("serial.send_file", connection_id=cid, path="env/boot.txt")
    await srv.call_ok(
        "serial.read_until", connection_id=cid, delimiter="L09-abcdef\r\n> ", timeout_ms=5000, max_bytes=4096
    )
    log = load_log(await srv.call_ok("serial.log.stop", connection_id=cid))
    await srv.call_ok("serial.close", connection_id=cid)

    records = log.jsonl
    starts = [i for i, r in enumerate(records) if r.get("event") == "send_file_start"]
    ends = [i for i, r in enumerate(records) if r.get("event") == "send_file_end"]
    check.expect(
        len(starts) == 2 and len(ends) == 2, f"two start and two end events (starts {starts}, ends {ends})"
    )
    if len(starts) != 2 or len(ends) != 2:
        return check
    check.note(f"start event: {records[starts[0]]}")
    check.note(f"end event:   {records[ends[0]]}")
    inside = [(i, r) for i, r in enumerate(records) if r.get("dir") == "tx" and starts[0] < i < ends[0]]
    outside_paced = [
        r
        for i, r in enumerate(records)
        if r.get("dir") == "tx" and not (starts[0] < i < ends[0]) and not (starts[1] < i < ends[1])
    ]
    sizes = [len(bytes.fromhex(r["hex"])) for _, r in inside]
    stamps = [r["t"] for _, r in inside]
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    wall_span = (
        datetime.fromisoformat(records[ends[0]]["ts"]) - datetime.fromisoformat(records[starts[0]]["ts"])
    ).total_seconds()
    mono_span = records[ends[0]]["t"] - records[starts[0]]["t"]
    check.note(
        f"paced transfer: {len(inside)} tx entries, sizes {sizes}, source {sorted({r['source'] for _, r in inside})}"
    )
    check.note(
        f"start->end: {mono_span:.3f} s by the log's monotonic `t`, {wall_span:.3f} s by its wall-clock `ts` (wall clock steps are not product delay)"
    )
    check.note(f"gaps between consecutive tx entries: {ms_stats(gaps) if gaps else 'n/a'}")
    check.expect(
        records[starts[0]]["path"] == "env/boot.txt" and records[ends[0]]["path"] == "env/boot.txt",
        "start and end events name the relative path",
    )
    check.expect(starts[0] < ends[0], "start event precedes end event")
    check.expect(
        sizes == [12] * 13 + [4], "tx segments are 13 x 12 bytes + 4 bytes (chunk_size 12 over 160 bytes)"
    )
    check.expect(
        b"".join(bytes.fromhex(r["hex"]) for _, r in inside) == payload,
        "the tx segments concatenate to the file",
    )
    check.expect(all(r["source"] == "mcp" for _, r in inside), "segments are attributed to source 'mcp'")
    check.expect(
        bool(gaps) and min(gaps) >= 0.018,
        f"every gap is paced: min {min(gaps) * 1000:.1f} ms >= 18 ms (chunk_delay_ms=20)"
        if gaps
        else "gaps exist",
    )
    check.expect(
        bool(gaps) and max(gaps) <= 0.250,
        f"no pause longer than 250 ms between segments (max {max(gaps) * 1000:.1f} ms)"
        if gaps
        else "gaps exist",
    )
    check.expect(not outside_paced, "no tx entry outside the two start..end windows")
    unpaced = [r for i, r in enumerate(records) if r.get("dir") == "tx" and starts[1] < i < ends[1]]
    check.note(
        f"control, same file without pacing: {len(unpaced)} tx entry of sizes {[len(bytes.fromhex(r['hex'])) for r in unpaced]} (response bytes_written={control['bytes_written']})"
    )
    check.expect(
        len(unpaced) == 1 and len(inside) > 1,
        "contrast: unpaced send_file is one segment, the paced one is many",
    )
    return check


# --------------------------------------------------------------------------- bullets 4 and 6 (MCP)


def step_table(result: dict[str, Any]) -> list[str]:
    return [
        f"line {s['line']}: kind={s['kind']} outcome={s['outcome']} matched={s['matched_text']!r} text={s['text']!r}"
        for s in result["steps"]
    ]


async def check_script_pass(srv: Any, device: Device, work: Path) -> tuple[Check, dict[str, Any]]:
    check = Check("4", "script with prompt waits off, explicit expects, passes through serial.script.run")
    opened = await open_device(srv, device)
    cid = opened["connection_id"]
    run = await srv.call_ok("serial.script.run", connection_id=cid, path="demo.script", wait_ms=30000)
    result = run["result"]
    check.note(f"run_id={run['run_id']} state={run['state']} result_path={run['result_path']}")
    for row in step_table(result):
        check.note(row)
    check.expect(run["state"] == "passed" and result["state"] == "passed", "state is passed")
    send_steps = [s for s in result["steps"] if s["kind"] == "sendline"]
    check.expect(
        len(send_steps) == 2 and all(s["outcome"] == "passed" for s in send_steps),
        "both @sendline steps passed (the contrast below shows what a prompt wait would have done)",
    )
    expects = [s["matched_text"] for s in result["steps"] if s["kind"] == "expect"]
    check.expect(expects == ["DemoDevice v1.0.0", "> ", "pong", "> "], f"explicit expects matched {expects}")
    path = Path(run["result_path"])
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    check.expect(on_disk == result, "the result file on disk equals the response's result")
    stop = await srv.call_ok("serial.log.stop", connection_id=cid)
    log = load_log(stop)
    check.note(
        f"device boot banner {device.boot!r} was consumed before the server opened the port (pyserial's open also flushes input)"
    )
    tx = log.stream("tx")
    check.note(f"port log: raw={log.raw!r}")
    check.expect(tx == b"version\r\nping\r\n", f"the log's tx bytes are exactly the two commands {tx!r}")
    check.expect(
        log.raw == b"DemoDevice v1.0.0\r\n> pong\r\n> ",
        "raw log == the two replies, each followed by the prompt",
    )
    # Control: with a prompt enabled, the wait after @sendline consumes the reply through "> ", so the following @expect pong
    # starves. The passing run above therefore shows that automatic prompt waits were really off.
    control = await srv.call_ok(
        "serial.script.run",
        connection_id=cid,
        source='@prompt "> "\n@timeout 2s\n@sendline ping\n@expect pong\n',
        wait_ms=30000,
    )
    control_failure = control["result"]["failure"]
    check.note(f"control, prompt enabled: state={control['state']} failure={control_failure}")
    check.expect(
        control["state"] == "failed" and control_failure is not None and control_failure["line"] == 4,
        "contrast: with @prompt \"> \" the wait after @sendline eats the reply and '@expect pong' (line 4) times out",
    )
    await srv.call_ok("serial.close", connection_id=cid)
    check.expect(result["failure"] is None and not result["marks"], "no failure recorded")
    return check, result


async def check_script_fail(srv: Any, device: Device, work: Path) -> Check:
    check = Check(
        "6",
        "same script with a wrong expected answer fails at that line; the port log holds the device's output",
    )
    opened = await open_device(srv, device)
    cid = opened["connection_id"]
    script = (work / "scripts" / "wrong.script").read_text(encoding="utf-8")
    check.note(f"line {WRONG_LINE} of wrong.script: {script.splitlines()[WRONG_LINE - 1]!r}")
    run = await srv.call_ok("serial.script.run", connection_id=cid, path="wrong.script", wait_ms=30000)
    result = run["result"]
    failure = result["failure"]
    for row in step_table(result):
        check.note(row)
    check.note(f"failure: {failure}")
    check.expect(run["state"] == "failed" and result["state"] == "failed", "state is failed")
    check.expect(failure is not None and failure["line"] == WRONG_LINE, f"failure.line == {WRONG_LINE}")
    check.expect(
        failure is not None and failure["text"] == script.splitlines()[WRONG_LINE - 1],
        "failure.text is the wrong @expect line",
    )
    check.expect(
        failure is not None and "DemoDevice v1.0.0" in failure["output_tail"],
        "the failure's output tail shows what the device really said",
    )
    check.expect(
        result["steps"][-1]["line"] == WRONG_LINE and result["steps"][-1]["outcome"] != "ok",
        "the last recorded step is the failing line",
    )
    stop = await srv.call_ok("serial.log.stop", connection_id=cid)
    log = load_log(stop)
    await srv.call_ok("serial.close", connection_id=cid)
    check.note(f"port log raw={log.raw!r} tx={log.stream('tx')!r}")
    check.expect(
        b"DemoDevice v1.0.0\r\n> " in log.raw, "the raw port log holds the device's reply to 'version'"
    )
    check.expect(
        any("DemoDevice v1.0.0" in r.get("text", "") for r in log.jsonl if r.get("dir") == "rx"),
        "the jsonl log holds it as rx",
    )
    check.expect(
        any(e["dir"] == "rx" and e["text"] == "DemoDevice v1.0.0" for e in log.lines),
        "the lines log holds it as a complete rx line",
    )
    check.expect(
        b"version" not in log.raw,
        "the demo device did not echo: 'version' is in tx only, never in the received bytes",
    )
    check.expect(b"version\r\n" in log.stream("tx"), "the sent command is in the jsonl tx entries")
    return check


# --------------------------------------------------------------------------- bullet 5 (CLI)


def cli_command() -> list[str]:
    entry = Path(sys.executable).with_name("serial_mcp_script")
    return [str(entry)] if entry.exists() else [sys.executable, "-m", "serial_mcp_server.cli"]


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SERIAL_MCP_")}
    env[TOKEN_ENV] = TOKEN
    return env


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*cli_command(), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd,
        env=clean_env(),
        check=False,
    )


def stderr_values(stderr: str, prefix: str) -> list[str]:
    return [line[len(prefix) :].strip() for line in stderr.splitlines() if line.startswith(prefix)]


def shape(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "top_keys": sorted(result),
        "step_keys": sorted({tuple(sorted(step)) for step in result["steps"]}),
        "connection_keys": sorted(result["connection"]),
        "steps": [
            (s["line"], s["text"], s["kind"], s["outcome"], s["matched_text"]) for s in result["steps"]
        ],
        "state": result["state"],
        "failure": result["failure"],
        "sha256": result["sha256"],
        "log_extensions": sorted(Path(p).suffix for p in result["log_paths"]),
    }


async def check_cli(device: Device, work: Path, mcp_result: dict[str, Any]) -> tuple[Check, Check]:
    check = Check(
        "5", "the same script passes through serial_mcp_script with result files of the same structure"
    )
    extra = Check(
        "6b",
        "(CLI) the wrong-answer script fails at that line with exit 1; the CLI's port log holds the device's output",
    )
    cwd = work / "cli"
    cwd.mkdir()
    check.note(
        f"command: {' '.join(cli_command())}; device boot banner {device.boot!r} was consumed before the CLI opened the port"
    )
    formats = [x for fmt in ("jsonl", "text", "lines", "raw") for x in ("--log-format", fmt)]
    logs = work / "cli-logs"
    done = await anyio.to_thread.run_sync(
        run_cli,
        [str(work / "scripts" / "demo.script"), "--port", device.path, "--log-dir", str(logs), *formats],
        cwd,
    )
    check.note(f"exit status {done.returncode}; stdout={done.stdout!r}")
    for line in done.stderr.splitlines():
        check.note(f"stderr| {line.replace(str(work), '<work>')}")
    check.expect(done.returncode == 0 and done.stdout == "", "exit status 0 and empty stdout")
    results = stderr_values(done.stderr, "Result:")
    check.expect(
        len(results) == 1 and Path(results[0]).is_file(), "a result file was written and named on stderr"
    )
    if results and Path(results[0]).is_file():
        cli_result = json.loads(Path(results[0]).read_text(encoding="utf-8"))
        a, b = shape(mcp_result), shape(cli_result)
        check.note(f"MCP shape: {a}")
        check.note(f"CLI shape: {b}")
        check.expect(cli_result["state"] == "passed", "CLI result state is passed")
        for key in (
            "top_keys",
            "step_keys",
            "steps",
            "state",
            "failure",
            "sha256",
            "log_extensions",
            "connection_keys",
        ):
            check.expect(
                a[key] == b[key], f"result {key} identical between serial.script.run and serial_mcp_script"
            )
        check.expect(
            Path(results[0]).parent == logs, "the result file sits in the log directory, as for the MCP tools"
        )
    # the wrong-answer script through the CLI, on the same (now idle) device
    done = await anyio.to_thread.run_sync(
        run_cli,
        [
            str(work / "scripts" / "wrong.script"),
            "--port",
            device.path,
            "--log-dir",
            str(work / "cli-logs-fail"),
            *formats,
        ],
        cwd,
    )
    extra.note(f"exit status {done.returncode}")
    for line in done.stderr.splitlines():
        extra.note(f"stderr| {line.replace(str(work), '<work>')}")
    extra.expect(done.returncode == 1, "exit status 1 (script failed)")
    results = stderr_values(done.stderr, "Result:")
    if results:
        failed = json.loads(Path(results[0]).read_text(encoding="utf-8"))
        extra.expect(
            failed["state"] == "failed" and failed["failure"]["line"] == WRONG_LINE,
            f"result file: failed at line {WRONG_LINE}",
        )
    else:
        extra.error("no result file named on stderr")
    raw_paths = [p for p in stderr_values(done.stderr, "Log:") if p.endswith(".bin")]
    if raw_paths:
        raw = Path(raw_paths[0]).read_bytes()
        extra.expect(
            b"DemoDevice v1.0.0\r\n> " in raw and b"version" not in raw,
            f"raw log holds the device's reply and no echo ({raw!r})",
        )
    else:
        extra.error("no raw log named on stderr")
    return check, extra


# --------------------------------------------------------------------------- bullet 7


def tree(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in sorted(root.rglob("*")) if p.is_file()}


async def check_defaults(work: Path, fleet: Fleet) -> Check:
    check = Check("7", "a server started without the new variables creates no log files outside script runs")
    cwd = work / "server-default"
    inherited = sorted(k for k in os.environ if k.startswith("SERIAL_MCP_"))
    check.note(f"server environment additions: none; SERIAL_MCP_* in this shell (not passed on): {inherited}")
    async with mcp_server({}, cwd, work / "stderr-default.txt") as srv:
        first, second = fleet.start(), fleet.start()
        opened = await srv.call_ok("serial.open", port=first.path)
        cid = opened["connection_id"]
        check.expect(
            "log" not in opened and "mirror" not in opened, "serial.open reports neither a log nor a mirror"
        )
        status = await srv.call_ok("serial.connection_status", connection_id=cid)
        logging = await srv.call_ok("serial.log.status", connection_id=cid)
        check.expect(
            "log" not in status and logging["logging"] is False,
            f"connection_status has no log; serial.log.status -> {logging}",
        )
        await srv.call_ok("serial.write", connection_id=cid, data="ping", append_newline=True)
        reply = await srv.call_ok(
            "serial.read_until", connection_id=cid, delimiter="pong\r\n> ", timeout_ms=3000
        )
        await srv.call_ok("serial.configure", connection_id=cid, inter_char_delay_ms=8)
        await srv.call_ok("serial.write", connection_id=cid, data="ping", append_newline=True)
        await srv.call_ok("serial.read_until", connection_id=cid, delimiter="pong\r\n> ", timeout_ms=3000)
        check.note(f"traffic without a script: reply {reply['data']!r}, a paced write, a read")
        before = tree(cwd)
        check.note(f"files under the server's working directory after plain traffic: {sorted(before)}")
        check.expect(
            not any(name.startswith(".serial_mcp/logs") for name in before)
            and not any("serial-mcp-log" in n for n in before),
            "no port log or result file exists",
        )

        script = '@prompt --off\n@timeout 3s\n@sendline ping\n@expect pong\n@expect "> "\n'
        run = await srv.call_ok("serial.script.run", connection_id=cid, source=script, wait_ms=20000)
        deadline = time.monotonic() + 3
        while (
            "log" in await srv.call_ok("serial.connection_status", connection_id=cid)
            and time.monotonic() < deadline
        ):
            await anyio.sleep(0.05)
        after_run = tree(cwd)
        created = sorted(set(after_run) - set(before))
        check.note(f"script run state={run['state']}; files created by the run: {created}")
        check.expect(run["state"] == "passed", "the inline script passed")
        check.expect(
            any(n.endswith(".txt") for n in created) and any(n.endswith(".json") for n in created),
            "the script run created a text port log and a result file",
        )
        check.expect(
            all(n.startswith(".serial_mcp/logs/") for n in created), "they are all under .serial_mcp/logs/"
        )

        await srv.call_ok("serial.write", connection_id=cid, data="ping", append_newline=True)
        await srv.call_ok("serial.read_until", connection_id=cid, delimiter="pong\r\n> ", timeout_ms=3000)
        status_after = await srv.call_ok("serial.connection_status", connection_id=cid)
        log_name = next(n for n in created if n.endswith(".txt"))
        await anyio.sleep(
            0.5
        )  # bounded settle: the log's disk thread flushes within milliseconds when a log is active
        grown = (cwd / log_name).stat().st_size - after_run[log_name]
        check.note(
            f"the log started by the run was closed when the run ended; connection_status after plain traffic: log present={'log' in status_after}; the run's text log grew by {grown} bytes"
        )
        check.expect(
            "log" not in status_after and grown == 0,
            "traffic after the script run is not logged anywhere (the run's own log was closed)",
        )
        opened2 = await srv.call_ok("serial.open", port=second.path)
        cid2 = opened2["connection_id"]
        await srv.call_ok("serial.write", connection_id=cid2, data="ping", append_newline=True)
        await srv.call_ok("serial.read_until", connection_id=cid2, delimiter="pong\r\n> ", timeout_ms=3000)
        after_traffic = tree(cwd)
        check.expect(
            set(after_traffic) == set(after_run) and "log" not in opened2,
            f"traffic after the script, and a second connection, created no further file (new files: {sorted(set(after_traffic) - set(after_run))})",
        )
        run2 = await srv.call_ok(
            "serial.script.run", connection_id=cid2, source=script, log=False, wait_ms=20000
        )
        created2 = sorted(set(tree(cwd)) - set(after_traffic))
        check.note(
            f"script run with log=false on the second connection: state={run2['state']}, new files {created2}"
        )
        check.expect(
            run2["state"] == "passed" and all(n.endswith(".json") for n in created2) and len(created2) == 1,
            "log=false creates only the script's result file, no port log",
        )
        await srv.call_ok("serial.close", connection_id=cid)
        await srv.call_ok("serial.close", connection_id=cid2)
    check.note(f"final tree: {sorted(tree(cwd))}")
    return check


# --------------------------------------------------------------------------- bullet 8 (regex time limit)


async def timed_status(srv: Any, run_id: str) -> tuple[dict[str, Any], float]:
    began = time.monotonic()
    status = await srv.call_ok("serial.script.status", run_id=run_id)
    return status, time.monotonic() - began


async def frozen_status(srv: Any, run_id: str) -> float:
    """Sensitivity control: SIGSTOP the server and time a status call across the freeze.

    Shows that the response times measured for bullet 8 would reveal a server that stops answering.
    """
    pid = find_server_pid()
    timed: list[float] = []

    async def call() -> None:
        _, seconds = await timed_status(srv, run_id)
        timed.append(seconds)

    os.kill(pid, signal.SIGSTOP)
    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        try:
            await anyio.sleep(STALL_CONTROL_SECONDS)
        finally:
            os.kill(pid, signal.SIGCONT)
    return timed[0]


async def check_regex_timeout(srv: Any, device: Device, work: Path) -> Check:
    check = Check(
        "8", "a catastrophic pattern ends the run errored at the regex limit while the server keeps answering"
    )
    opened = await open_device(srv, device)
    cid = opened["connection_id"]
    started = time.monotonic()
    status = await srv.call_ok("serial.script.run", connection_id=cid, path="catastrophic.script")
    run_id = status["run_id"]
    latencies: list[float] = []
    while status["state"] == "running" and time.monotonic() - started < 60:
        await anyio.sleep(0.05)
        status, seconds = await timed_status(srv, run_id)
        latencies.append(seconds)
    elapsed = time.monotonic() - started
    result = status.get("result") or {}
    failure = result.get("failure") or {}
    check.note(
        f"run ended {status['state']} after {elapsed:.2f} s; {len(latencies)} status calls while it ran"
    )
    check.note(f"slowest status call {max(latencies, default=0.0) * 1000:.0f} ms; failure: {failure}")
    check.expect(
        status["state"] == "errored" and result.get("reason") == "regex_timeout",
        "the run ends errored with the reason regex_timeout",
    )
    check.expect(failure.get("line") == CATASTROPHIC_LINE, f"failure.line == {CATASTROPHIC_LINE}")
    check.expect(
        f"line {CATASTROPHIC_LINE}" in (failure.get("detail") or ""),
        "the failure detail names the line of the pattern",
    )
    check.expect(
        PATTERN_TIMEOUT_SECONDS * 0.9 <= elapsed < PATTERN_TIMEOUT_SECONDS + 10,
        f"the run ended near the {PATTERN_TIMEOUT_SECONDS:g} s limit, not at the script's 60 s step timeout",
    )
    check.expect(len(latencies) >= 10, "the server was polled while the pattern ran")
    check.expect(
        max(latencies, default=RESPONSE_LIMIT_SECONDS) < RESPONSE_LIMIT_SECONDS,
        f"every status call answered within {RESPONSE_LIMIT_SECONDS:g} s while the pattern ran",
    )
    frozen = await frozen_status(srv, run_id)
    check.expect(
        frozen >= STALL_CONTROL_SECONDS * 0.9,
        f"control: a server frozen for {STALL_CONTROL_SECONDS:g} s shows as a {frozen:.2f} s call",
    )
    await srv.call_ok("serial.close", connection_id=cid)
    return check


async def check_regex_timeout_cli(device: Device, work: Path) -> Check:
    check = Check("8b", "(CLI) the same script ends with exit status 2 at the regex limit")
    cwd = work / "cli-regex"
    cwd.mkdir()
    started = time.monotonic()
    done = await anyio.to_thread.run_sync(
        run_cli,
        [
            str(work / "scripts" / "catastrophic.script"),
            "--port",
            device.path,
            "--log-dir",
            str(work / "cli-logs-regex"),
        ],
        cwd,
    )
    elapsed = time.monotonic() - started
    check.note(f"exit status {done.returncode} after {elapsed:.2f} s")
    for line in done.stderr.splitlines():
        check.note(f"stderr| {line.replace(str(work), '<work>')}")
    check.expect(done.returncode == 2, "exit status 2 (the run errored)")
    check.expect(
        elapsed < PATTERN_TIMEOUT_SECONDS + 15, "the command ended near the limit, not at the step timeout"
    )
    results = stderr_values(done.stderr, "Result:")
    if results:
        result = json.loads(Path(results[0]).read_text(encoding="utf-8"))
        check.expect(
            result["state"] == "errored" and result["reason"] == "regex_timeout",
            "result file: errored with the reason regex_timeout",
        )
    else:
        check.error("no result file named on stderr")
    return check


# --------------------------------------------------------------------------- driver


async def guarded(report: Report, ident: str, title: str, coro: Any) -> Any:
    """Run one scenario; a crash becomes a FAIL with the traceback instead of aborting the run."""
    try:
        return await coro
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        crash = Check(ident, f"{title} (scenario crashed)")
        crash.error(f"{type(exc).__name__}: {exc}")
        crash.lines.extend("      | " + line for line in traceback.format_exc().splitlines())
        report.add(crash)
        return None


async def run_all_bounded(work: Path, fleet: Fleet, report: Report) -> dict[str, Check]:
    """Every scenario, within the harness's overall deadline."""
    with anyio.fail_after(RUN_DEADLINE_SECONDS):
        return await run_all(work, fleet, report)


async def run_all(work: Path, fleet: Fleet, report: Report) -> dict[str, Check]:
    (work / "scripts").mkdir()
    (work / "scripts" / "demo.script").write_text(PASS_SCRIPT, encoding="utf-8")
    (work / "scripts" / "wrong.script").write_text(WRONG_SCRIPT, encoding="utf-8")
    (work / "scripts" / "catastrophic.script").write_text(CATASTROPHIC_SCRIPT, encoding="utf-8")
    verdicts: dict[str, Check] = {}

    def add(check: Check | None) -> None:
        if check is not None:
            verdicts[check.ident] = check
            report.add(check)

    mcp_result: dict[str, Any] | None = None
    async with mcp_server(server_env(work), work / "server", work / "stderr-main.txt") as srv:
        # bullet 3
        add(await guarded(report, "3", "send_file", check_send_file(srv, fleet.start(), work)))

        # bullets 1 and 2: 4 KiB paste typed at 8 ms per character
        c1 = Check(
            "1",
            "4 KiB paste into the mirror terminal at 8 ms/char does not delay device output in serial.read",
        )
        c2 = Check("2", "jsonl, text, lines and raw files agree on the received bytes and line boundaries")
        try:
            log, oracle, m = await run_paste(srv, fleet.start(), burst=False, check=c1)
            analyze_terminal_paste(c1, m)
            analyze_log_agreement(c2, log, oracle, m["typed"])
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            c1.error(f"scenario crashed: {type(exc).__name__}: {exc}")
            c1.lines.extend("      | " + line for line in traceback.format_exc().splitlines())
        add(c1)
        add(c2)

        # bullet 1, harder variant: burst paste with the writer busy in an 8 ms/char paced write for ~33 s
        c1b = Check(
            "1b",
            "4 KiB burst paste, wire paced at 8 ms/char by the connection, does not delay device output in serial.read",
        )
        try:
            log_b, oracle_b, m_b = await run_paste(srv, fleet.start(), burst=True, check=c1b)
            analyze_burst_paste(c1b, m_b, log_b)
            c2b = Check("2b", "jsonl, text, lines and raw files agree for the burst paste connection too")
            analyze_log_agreement(c2b, log_b, oracle_b, m_b["typed"])
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            c1b.error(f"scenario crashed: {type(exc).__name__}: {exc}")
            c1b.lines.extend("      | " + line for line in traceback.format_exc().splitlines())
            c2b = None
        add(c1b)
        add(c2b)

        # bullets 4 and 6
        outcome = await guarded(report, "4", "script pass", check_script_pass(srv, fleet.start(), work))
        if outcome is not None:
            check4, mcp_result = outcome
            add(check4)
        add(await guarded(report, "6", "script fail", check_script_fail(srv, fleet.start(), work)))

        # bullet 8, through the server
        add(await guarded(report, "8", "regex timeout", check_regex_timeout(srv, fleet.start(), work)))

    # bullet 5 (the server is gone, so nothing else holds a device)
    if mcp_result is None:
        skipped = Check(
            "5", "the same script passes through serial_mcp_script with result files of the same structure"
        )
        skipped.error("skipped: bullet 4 produced no MCP result to compare against")
        add(skipped)
    else:
        pair = await guarded(report, "5", "cli", check_cli(fleet.start(), work, mcp_result))
        if pair is not None:
            add(pair[0])
            add(pair[1])

    # bullet 8, through the command line
    add(await guarded(report, "8b", "regex timeout cli", check_regex_timeout_cli(fleet.start(), work)))

    # bullet 7
    add(await guarded(report, "7", "defaults", check_defaults(work, fleet)))
    return verdicts


BULLETS = [
    ("1", ["1", "1b"], "4 KiB paste at 8 ms/char does not delay serial.read output"),
    ("2", ["2", "2b"], "jsonl/text/lines/raw agree on bytes and line boundaries"),
    ("3", ["3"], "send_file chunk pacing: paced tx segments between start and end"),
    ("4", ["4"], "script with prompt waits off passes via serial.script.run"),
    ("5", ["5"], "same script passes via serial_mcp_script, same result structure"),
    ("6", ["6", "6b"], "wrong expected answer fails at that line; log holds device output"),
    ("7", ["7"], "no log files without the new variables, outside script runs"),
    ("8", ["8", "8b"], "a catastrophic pattern ends the run errored at the regex limit; the server answers"),
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--keep-work", action="store_true", help="keep the temporary directory for inspection"
    )
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="integration-harness-"))
    fleet = Fleet()
    report = Report()
    verdicts: dict[str, Check] = {}
    started = time.monotonic()
    print(f"integration harness: repo {ROOT}\nwork dir {work}; python {sys.version.split()[0]}", flush=True)
    try:
        verdicts = anyio.run(run_all_bounded, work, fleet, report)
    except BaseException:
        print("HARNESS ERROR\n" + traceback.format_exc(), flush=True)
        report.add(Check("harness", "run aborted", failures=1))
    finally:
        fleet.stop_all()
        strays = stray_processes()
        if strays:
            print("STRAY PROCESSES (not reaped):", *strays, sep="\n  ")
            report.add(Check("reap", "all child processes reaped", failures=1))
        if args.keep_work:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n==== summary ({time.monotonic() - started:.0f} s) ====")
    for ident, parts, title in BULLETS:
        found = [verdicts[p] for p in parts if p in verdicts]
        ok = len(found) == len(parts) and all(c.ok for c in found)
        verdict = ", ".join(f"{c.ident}={'PASS' if c.ok else 'FAIL'}" for c in found)
        print(f"bullet {ident}: {'PASS' if ok else 'FAIL'}  ({verdict})  {title}")
    return 0 if report.all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
