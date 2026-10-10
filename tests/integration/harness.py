"""Infrastructure for the end-to-end harness: report, devices, terminal, MCP client, log parsing.

Drives real processes and real pseudo-terminals. Used by run.py; not collected by pytest.
"""

from __future__ import annotations

import base64
import bisect
import json
import os
import select
import subprocess
import sys
import threading
import time
import tty
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import anyio.abc
import anyio.to_thread
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
DEMO_DEVICE = ROOT / "examples" / "demo-device" / "pty_server.py"
TOKEN_ENV = "INTEGRATION_HARNESS_TOKEN"
TOKEN = f"{os.getpid()}-{int(time.time())}"
CALL_DEADLINE_S = 180


# --------------------------------------------------------------------------- report


@dataclass
class Check:
    """One bullet's verdict with the evidence behind it."""

    ident: str
    title: str
    lines: list[str] = field(default_factory=list)
    failures: int = 0

    @property
    def ok(self) -> bool:
        return self.failures == 0

    def note(self, text: str) -> None:
        """Record an observation."""
        self.lines.append(f"      observed: {text}")

    def expect(self, condition: bool, text: str) -> bool:
        """Record one assertion; any failed assertion fails the check."""
        self.lines.append(f"      [{'ok' if condition else 'FAILED'}] {text}")
        if not condition:
            self.failures += 1
        return condition

    def error(self, text: str) -> None:
        self.lines.append(f"      [ERROR] {text}")
        self.failures += 1

    def render(self) -> str:
        head = f"{'PASS' if self.ok else 'FAIL'}  [{self.ident}] {self.title}"
        return "\n".join([head, *self.lines])


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def add(self, check: Check) -> None:
        self.checks.append(check)
        print(check.render(), flush=True)

    @property
    def all_ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * fraction // 1))
    return ordered[int(rank) - 1]


def ms_stats(values: list[float]) -> str:
    """min / median / p95 / max in milliseconds."""
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    return (
        f"n={len(values)} min={ordered[0] * 1000:.1f} median={median * 1000:.1f} "
        f"p95={percentile(values, 0.95) * 1000:.1f} max={ordered[-1] * 1000:.1f} ms"
    )


# --------------------------------------------------------------------------- devices


class Device:
    """A real examples/demo-device/pty_server.py process."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(DEMO_DEVICE)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, TOKEN_ENV: TOKEN},
            bufsize=0,
        )
        self.path = self._read_path()
        self.boot = self._drain_boot()

    def _read_path(self) -> str:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        seen = b""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                seen += chunk
                for line in seen.decode(errors="replace").splitlines():
                    if line.startswith("Connect to:"):
                        return line.split(":", 1)[1].strip()
        self.stop()
        raise RuntimeError(f"demo device never printed its path; saw {seen!r}")

    def _drain_boot(self) -> bytes:
        """Consume the boot banner through a private descriptor, so no later open can race with it.

        The device already put its slave in raw mode; changing termios here (tty.setraw flushes input) would drop the banner.
        """
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY)
        try:
            data = b""
            deadline = time.monotonic() + 5
            while not data.endswith(b"> ") and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    data += os.read(fd, 4096)
        finally:
            os.close(fd)
        if not data.endswith(b"> "):
            self.stop()
            raise RuntimeError(f"demo device never finished its boot banner; saw {data!r}")
        return data

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(3)
        if self.proc.stdout is not None:
            self.proc.stdout.close()


class Fleet:
    """Owns every demo device so that all are reaped, even on failure."""

    def __init__(self) -> None:
        self.devices: list[Device] = []

    def start(self) -> Device:
        device = Device()
        self.devices.append(device)
        return device

    def stop_all(self) -> None:
        for device in self.devices:
            device.stop()


def stray_processes() -> list[str]:
    """Command lines of live processes that still carry this run's environment token."""
    marker = f"{TOKEN_ENV}={TOKEN}".encode()
    strays = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            environ = (entry / "environ").read_bytes()
            if marker in environ.split(b"\0"):
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                strays.append(f"pid {entry.name}: {cmdline.strip()}")
        except OSError:
            continue
    return strays


def find_server_pid() -> int:
    """Pid of the MCP server child of this run (the one process with our token running ``-m serial_mcp_server``)."""
    marker = f"{TOKEN_ENV}={TOKEN}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes().split(b"\0")
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if marker in environ and b"serial_mcp_server" in cmdline:
            found.append(int(entry.name))
    if len(found) != 1:
        raise RuntimeError(f"expected one server process, found {found}")
    return found[0]


# --------------------------------------------------------------------------- terminal emulator


class Paste:
    """Writes bytes into a PTY on its own thread, optionally one byte per ``delay_s``."""

    def __init__(self, fd: int, data: bytes, delay_s: float) -> None:
        self.data = data
        self.delay_s = delay_s
        self.line_done: list[float] = []  # wall time right after each newline byte was written
        self.started_at = 0.0
        self.finished_at = 0.0
        self.error: BaseException | None = None
        self._fd = fd
        self._thread = threading.Thread(target=self._run, name="paste", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            start = time.monotonic()
            self.started_at = time.monotonic()
            if self.delay_s <= 0:
                view = memoryview(self.data)
                while view:
                    view = view[os.write(self._fd, view) :]
                if self.data.endswith(b"\n"):
                    self.line_done.append(time.monotonic())
            else:
                for index, byte in enumerate(self.data):
                    os.write(self._fd, bytes([byte]))
                    if byte == 0x0A:
                        self.line_done.append(time.monotonic())
                    remaining = start + (index + 1) * self.delay_s - time.monotonic()
                    if remaining > 0 and index + 1 < len(self.data):
                        time.sleep(remaining)
            self.finished_at = time.monotonic()
        except BaseException as exc:
            self.error = exc

    async def wait(self, timeout: float) -> None:
        await anyio.to_thread.run_sync(self._thread.join, timeout)
        if self._thread.is_alive():
            raise TimeoutError("paste thread did not finish")
        if self.error is not None:
            raise self.error


class Terminal:
    """Acts as a terminal emulator attached to the mirror's PTY slave."""

    def __init__(self, path: str) -> None:
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        self.received = bytearray()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._drain, name="terminal-drain", daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while not self._stop.is_set():
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.fd, 4096)
                except OSError:
                    return
                self.received += data

    def paste(self, data: bytes, delay_s: float) -> Paste:
        return Paste(self.fd, data, delay_s)

    def close(self) -> None:
        self._stop.set()
        self._reader.join(2)
        os.close(self.fd)


# --------------------------------------------------------------------------- MCP client


class Client:
    """Thin wrapper over an MCP ClientSession that returns the tool's JSON payload."""

    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        with anyio.fail_after(CALL_DEADLINE_S):
            result = await self.session.call_tool(tool, arguments)
        text = "".join(block.text for block in result.content if block.type == "text")
        if result.isError:
            raise RuntimeError(f"{tool} protocol error: {text}")
        return json.loads(text)

    async def call_ok(self, tool: str, **arguments: Any) -> dict[str, Any]:
        payload = await self.call(tool, **arguments)
        if not payload.get("ok"):
            raise RuntimeError(f"{tool} failed: {payload}")
        return payload


@asynccontextmanager
async def mcp_server(env: dict[str, str], cwd: Path, stderr_path: Path) -> AsyncIterator[Client]:
    """Run the real server as an MCP stdio subprocess with exactly the given environment additions."""
    cwd.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "serial_mcp_server"],
        env={**env, TOKEN_ENV: TOKEN},
        cwd=cwd,
    )
    with stderr_path.open("w", encoding="utf-8") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield Client(session)


class Probe:
    """Keeps calling serial.read and stamps every non-empty return, like an agent polling for output."""

    def __init__(self, client: Client, connection_id: str) -> None:
        self.client = client
        self.connection_id = connection_id
        self.data = bytearray()
        self.times: list[float] = []
        self.totals: list[int] = []
        self.calls = 0
        self._stop = False
        self._stopped = anyio.Event()

    def start(self, task_group: anyio.abc.TaskGroup) -> None:
        """Poll in *task_group*; a failed call fails the group, and with it the scenario."""
        task_group.start_soon(self._run)

    async def _run(self) -> None:
        try:
            await self._poll()
        finally:
            self._stopped.set()

    async def _poll(self) -> None:
        while not self._stop:
            payload = await self.client.call_ok(
                "serial.read",
                connection_id=self.connection_id,
                nbytes=65536,
                timeout_ms=50,
                **{"as": "base64"},
            )
            stamp = time.monotonic()
            self.calls += 1
            if payload["n_read"]:
                self.data += base64.b64decode(payload["data"])
                self.times.append(stamp)
                self.totals.append(len(self.data))

    def time_reaching(self, total: int) -> float | None:
        """Wall time of the first serial.read return that brought the cumulative byte count to ``total``."""
        index = bisect.bisect_left(self.totals, total)
        return self.times[index] if index < len(self.totals) else None

    async def wait_total(self, total: int, timeout: float) -> float | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            arrival = self.time_reaching(total)
            if arrival is not None:
                return arrival
            await anyio.sleep(0.005)
        return self.time_reaching(total)

    async def stop(self) -> None:
        self._stop = True
        await self._stopped.wait()


# --------------------------------------------------------------------------- port log parsing


@dataclass
class LogFiles:
    paths: dict[str, str]
    raw: bytes
    text: bytes
    jsonl: list[dict[str, Any]]
    lines: list[dict[str, Any]]

    def stream(self, direction: str) -> bytes:
        """Bytes of one direction as the jsonl records carry them."""
        return b"".join(bytes.fromhex(r["hex"]) for r in self.jsonl if r.get("dir") == direction)

    def spans(self, direction: str) -> list[tuple[int, int, str, float]]:
        """(start, end, ts, t) of every jsonl chunk in one direction, in byte offsets of that direction's stream."""
        spans = []
        position = 0
        for record in self.jsonl:
            if record.get("dir") == direction:
                size = len(bytes.fromhex(record["hex"]))
                spans.append((position, position + size, record["ts"], record["t"]))
                position += size
        return spans

    def events(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.jsonl if r.get("event") == name]


def _jsonl(path: str) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def load_log(stop_payload: dict[str, Any]) -> LogFiles:
    """Read every file named by a serial.log.stop response."""
    paths = {entry["format"]: entry["path"] for entry in stop_payload["files"]}
    return LogFiles(
        paths=paths,
        raw=Path(paths["raw"]).read_bytes(),
        text=Path(paths["text"]).read_bytes(),
        jsonl=_jsonl(paths["jsonl"]),
        lines=_jsonl(paths["lines"]),
    )


def align_lines(
    entries: list[dict[str, Any]], stream: bytes, spans: list[tuple[int, int, str, float]]
) -> list[str]:
    """Walk a ``lines`` file against the byte stream; return every disagreement.

    Each entry must hold exactly the bytes at the current position, a non-partial entry must be followed by a
    line terminator, and the entry's ``ts``/``t`` must equal those of the jsonl chunk carrying its first byte.
    """
    problems: list[str] = []
    starts = [span[0] for span in spans]
    position = 0
    for index, entry in enumerate(entries):
        text = entry["text"].encode("utf-8")
        if stream[position : position + len(text)] != text:
            problems.append(f"entry {index}: text {entry['text']!r} is not the stream at byte {position}")
            return problems
        start = position
        position += len(text)
        if not entry.get("partial"):
            if stream[position : position + 2] == b"\r\n":
                position += 2
            elif stream[position : position + 1] == b"\n":
                position += 1
            else:
                problems.append(f"entry {index}: no line terminator after byte {position}")
                return problems
        span = spans[bisect.bisect_right(starts, start) - 1]
        if (entry["ts"], entry["t"]) != (span[2], span[3]):
            problems.append(
                f"entry {index}: stamp {entry['ts']}/{entry['t']} differs from the chunk holding its first byte {span[2]}/{span[3]}"
            )
    if position != len(stream):
        problems.append(f"lines cover {position} of {len(stream)} stream bytes")
    return problems
