"""In-memory state for serial connections and script runs."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import serial as pyserial

from serial_mcp_server.mirror import ReaderThread, SerialBuffer
from serial_mcp_server.portlog import PortLogSlot
from serial_mcp_server.script.runner import RunResult, ScriptRunner
from serial_mcp_server.writer import SerialWriter

SCRIPT_CANCEL_WAIT_SECONDS = 5.0
logger = logging.getLogger("serial_mcp_server")


@dataclass
class SerialConnection:
    """A managed serial port connection."""

    connection_id: str
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    timeout: float  # seconds
    write_timeout: float  # seconds
    encoding: str
    newline: str
    ser: pyserial.Serial
    writer: SerialWriter
    buffer: SerialBuffer = field(default_factory=SerialBuffer)
    reader: ReaderThread | None = None
    opened_at: float = field(default_factory=time.time)
    last_seen_ts: float = field(default_factory=time.time)
    spec: dict[str, Any] | None = None

    @property
    def log_slot(self) -> PortLogSlot:
        """The port log holder shared with this connection's writer and reader."""
        return self.writer.log_slot


@dataclass
class ScriptRun:
    """One active or recently finished script run."""

    run_id: str
    connection_id: str
    started_at: float
    runner: ScriptRunner
    result: RunResult | None = None
    result_path: Path | None = None
    finished_at: float | None = None
    finished: threading.Event = field(default_factory=threading.Event)


class ScriptJobs:
    """Track connection claims and the process's most recent script runs."""

    _MAX_FINISHED = 20

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[str, str] = {}
        self._active: dict[str, ScriptRun] = {}
        self._finished: OrderedDict[str, ScriptRun] = OrderedDict()

    def claim(self, connection_id: str, runner: ScriptRunner) -> tuple[ScriptRun | None, str | None]:
        """Atomically claim a connection or return the id of its current owner."""
        with self._lock:
            existing_id = self._claims.get(connection_id)
            if existing_id is not None:
                return None, existing_id
            run = ScriptRun(uuid.uuid4().hex, connection_id, time.monotonic(), runner)
            self._claims[connection_id] = run.run_id
            self._active[run.run_id] = run
            return run, None

    def active_for_connection(self, connection_id: str) -> ScriptRun | None:
        """Return the active run that owns *connection_id*, if any."""
        with self._lock:
            run_id = self._claims.get(connection_id)
            return self._active.get(run_id) if run_id is not None else None

    def find(self, run_id: str) -> ScriptRun | None:
        """Return an active or retained finished run by id."""
        with self._lock:
            return self._active.get(run_id) or self._finished.get(run_id)

    def latest_for_connection(self, connection_id: str) -> ScriptRun | None:
        """Return the active run or newest retained finished run for a connection."""
        with self._lock:
            run_id = self._claims.get(connection_id)
            if run_id is not None:
                return self._active.get(run_id)
            return next(
                (run for run in reversed(self._finished.values()) if run.connection_id == connection_id),
                None,
            )

    def watch(self, run: ScriptRun, on_finish: Callable[[], None]) -> None:
        """Wait for a runner off-thread, clean up, then release its claim."""
        watcher = threading.Thread(
            target=self._watch,
            args=(run, on_finish),
            name=f"serial-script-watch-{run.run_id[:8]}",
            daemon=True,
        )
        watcher.start()

    def _watch(self, run: ScriptRun, on_finish: Callable[[], None]) -> None:
        try:
            run.runner.join()
        except Exception:
            logger.warning("Waiting for script run %s failed.", run.run_id, exc_info=True)
        try:
            on_finish()
        except Exception:
            logger.warning("Script run %s cleanup failed.", run.run_id, exc_info=True)
        finally:
            self.finish(run.run_id)

    def finish(self, run_id: str) -> ScriptRun | None:
        """Release a completed run's claim and retain its result."""
        with self._lock:
            run = self._active.pop(run_id, None)
            if run is None:
                return self._finished.get(run_id)
            if self._claims.get(run.connection_id) == run_id:
                del self._claims[run.connection_id]
            run.result = run.runner.result
            run.result_path = run.runner.result_path
            run.finished_at = time.monotonic()
            self._finished[run_id] = run
            while len(self._finished) > self._MAX_FINISHED:
                self._finished.popitem(last=False)
            run.finished.set()
            return run

    def release_unstarted(self, run_id: str) -> None:
        """Release a claim when setup fails before the runner has started."""
        with self._lock:
            run = self._active.pop(run_id, None)
            if run is None:
                return
            if self._claims.get(run.connection_id) == run_id:
                del self._claims[run.connection_id]
            run.finished_at = time.monotonic()
            run.finished.set()

    def cancel_all(self) -> None:
        """Request cancellation of every active run without waiting for completion."""
        with self._lock:
            runs = tuple(self._active.values())
        for run in runs:
            try:
                run.runner.cancel()
            except Exception:
                logger.warning("Could not cancel script run %s during shutdown.", run.run_id, exc_info=True)


class SerialState:
    """Shared connection and script state used by all tool handlers.

    Connection registry mutations happen on the asyncio event loop. ScriptJobs
    uses a lock because its runner watchers finish on background threads.
    Reader threads synchronize their buffer and serial-port access separately.
    """

    def __init__(self, max_connections: int = 10) -> None:
        self.connections: dict[str, SerialConnection] = {}
        self.max_connections = max_connections
        self.script_jobs = ScriptJobs()

    def generate_id(self) -> str:
        return f"s{uuid.uuid4().hex[:8]}"

    def get_connection(self, connection_id: str) -> SerialConnection:
        if connection_id not in self.connections:
            raise KeyError(
                f"Unknown connection_id: {connection_id}. "
                "Call serial.connections.list to see active connections."
            )
        return self.connections[connection_id]

    def add_connection(self, conn: SerialConnection) -> None:
        if len(self.connections) >= self.max_connections:
            raise RuntimeError(
                f"Maximum connections ({self.max_connections}) reached. "
                "Close a connection first. Set SERIAL_MCP_MAX_CONNECTIONS to adjust."
            )
        self.connections[conn.connection_id] = conn

    def remove_connection(self, connection_id: str) -> SerialConnection:
        if connection_id not in self.connections:
            raise KeyError(
                f"Unknown connection_id: {connection_id}. "
                "Call serial.connections.list to see active connections."
            )
        return self.connections.pop(connection_id)

    async def shutdown(self) -> None:
        """Cancel script runs, then stop all readers, writers, and open serial ports."""
        self.script_jobs.cancel_all()
        conns = list(self.connections.values())
        self.connections.clear()
        await asyncio.to_thread(stop_connections, conns)


def stop_connections(conns: list[SerialConnection]) -> None:
    """Signal every connection's threads before joining any, then close the logs and ports."""
    for conn in conns:
        conn.writer.stop()
        if conn.reader is not None:
            conn.reader.signal_stop()
    for conn in conns:
        conn.writer.join()
        if conn.reader is not None:
            try:
                conn.reader.join()
            except Exception:
                logger.warning("Reader for %s did not stop cleanly.", conn.port, exc_info=True)
        try:
            conn.log_slot.close_log()
        except Exception:
            logger.warning("Closing the port log for %s failed.", conn.port, exc_info=True)
        try:
            if conn.ser.is_open:
                conn.ser.close()
        except Exception:
            logger.warning("Closing %s failed.", conn.port, exc_info=True)
