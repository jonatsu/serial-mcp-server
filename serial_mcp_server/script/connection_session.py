"""Adapt a managed serial connection to the script engine's port interface.

Shared by the ``serial.script.*`` MCP tools and the ``serial_mcp_script`` command. This module never
imports ``mcp``.
"""

from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from serial_mcp_server.confined_files import MAX_CONFINED_FILE_BYTES, read_confined_file
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.pacing import Pacing
from serial_mcp_server.port.writer import SOURCE_MCP, UNPACED_SEGMENT_BYTES, WriteAborted
from serial_mcp_server.portlog.formats import SEND_FILE_END_EVENT, SEND_FILE_START_EVENT
from serial_mcp_server.script.matcher import OutputMatcher

_WRITE_WAIT_SECONDS = 0.05


class ConnectionSession:
    """Adapt a managed serial connection to the script engine's port interface."""

    def __init__(self, conn: SerialConnection, files_root: Path) -> None:
        self._conn = conn
        self._files_root = files_root
        # The run pins encoding at runner construction and newline at run start;
        # script claims block serial.configure, so capture-time values are stable.
        self.newline = conn.newline
        self.encoding = conn.encoding

    def _submit(self, payload: bytes, pacing: Pacing | None, cancel: threading.Event) -> int:
        job = self._conn.writer.submit(payload, pacing, source=SOURCE_MCP)
        # Poll instead of blocking on result(): this is the runner thread, and it must
        # wake periodically to forward the run's cancel event to the writer job.
        while True:
            if cancel.is_set():
                if job.future.cancel():  # still queued: the writer skips it, so none of it is written
                    raise WriteAborted("Write cancelled before it started.", 0)
                job.cancel.set()
            try:
                return job.future.result(timeout=_WRITE_WAIT_SECONDS)
            except concurrent.futures.TimeoutError:
                # Python 3.11+ aliases this to the builtin TimeoutError, so a job that failed with
                # one lands here too; a finished future re-raises its own error instead of polling on.
                if job.future.done():
                    return job.future.result()

    def subscribe(self, matcher: OutputMatcher) -> Callable[[], None]:
        if self._conn.reader is None:
            raise RuntimeError("The serial connection has no reader thread.")
        return self._conn.reader.subscribe(matcher)

    def write(self, data: bytes, pacing: Pacing | None, cancel: threading.Event) -> int:
        return self._submit(data, pacing, cancel)

    def send_file(self, path: Path, pacing: Pacing | None, cancel: threading.Event) -> int:
        relative = path.relative_to(self._files_root.resolve()).as_posix()
        _resolved, payload = read_confined_file(self._files_root, relative, MAX_CONFINED_FILE_BYTES)
        effective_pacing = self._conn.writer.pacing if pacing is None else pacing
        if not effective_pacing.is_paced:
            effective_pacing = Pacing(chunk_size=UNPACED_SEGMENT_BYTES)
        self.log_event(SEND_FILE_START_EVENT, path=relative, size=len(payload))
        try:
            written = self._submit(payload, effective_pacing, cancel)
        except WriteAborted as exc:
            self.log_event(SEND_FILE_END_EVENT, path=relative, bytes_written=exc.bytes_written)
            raise
        except Exception:
            self.log_event(SEND_FILE_END_EVENT, path=relative, bytes_written=0)
            raise
        self.log_event(SEND_FILE_END_EVENT, path=relative, bytes_written=written)
        return written

    def set_line(self, line: Literal["dtr", "rts"], value: bool) -> None:
        setattr(self._conn.ser, line, value)
        self._conn.log_slot.event(line, value=value)

    def log_event(self, name: str, **fields: Any) -> None:
        self._conn.log_slot.event(name, **fields)
