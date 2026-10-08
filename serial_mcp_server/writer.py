"""Single writer thread per serial connection.

Every byte sent to a port goes through one ``SerialWriter``: MCP write tools
and the PTY mirror both submit jobs to it. The reader thread never writes, so
a slow paced write cannot delay receiving.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("serial_mcp_server")

SOURCE_MCP = "mcp"
SOURCE_MIRROR = "mirror"

JOIN_TIMEOUT_S = 3.0


class WriterClosed(Exception):
    """The writer was stopped, so it accepts or runs no more jobs."""


class WriteAborted(Exception):
    """A write stopped before its payload was fully sent.

    Attributes:
        bytes_written: Bytes the port accepted before the write stopped.
    """

    def __init__(self, message: str, bytes_written: int) -> None:
        super().__init__(message)
        self.bytes_written = bytes_written


@dataclass(frozen=True)
class Pacing:
    """How a payload is split and delayed on its way to the port."""

    inter_char_delay_ms: int = 0
    chunk_size: int = 0
    chunk_delay_ms: int = 0

    @property
    def is_paced(self) -> bool:
        return bool(self.inter_char_delay_ms or self.chunk_size)


NO_PACING = Pacing()


@dataclass(eq=False)
class WriteJob:
    """One queued write. ``future`` resolves to the number of bytes written."""

    payload: bytes
    pacing: Pacing
    source: str
    future: Future[int] = field(default_factory=Future)
    cancel: threading.Event = field(default_factory=threading.Event)


def write_serial_payload(
    ser: Any, payload: bytes, pacing: Pacing, cancel: threading.Event | None = None
) -> int:
    """Write bytes to a serial port, pausing between drained segments when paced.

    Returns:
        The number of bytes the port accepted.

    Raises:
        WriteAborted: *cancel* was set before the payload was fully sent.
    """
    if pacing.inter_char_delay_ms:
        write_size, delay_ms = 1, pacing.inter_char_delay_ms
    elif pacing.chunk_size:
        write_size, delay_ms = pacing.chunk_size, pacing.chunk_delay_ms
    else:
        return ser.write(payload)

    cancel = cancel or threading.Event()
    n_written = 0
    for offset in range(0, len(payload), write_size):
        if cancel.is_set():
            raise WriteAborted("Write cancelled.", n_written)
        segment = payload[offset : offset + write_size]
        n = ser.write(segment)
        n_written += n
        ser.flush()
        if n != len(segment):
            break
        if offset + write_size < len(payload) and cancel.wait(delay_ms / 1000.0):
            raise WriteAborted("Write cancelled.", n_written)
    return n_written


def _fail(job: WriteJob, exc: BaseException) -> None:
    """Resolve *job* with an error unless its caller already cancelled it."""
    try:
        job.future.set_exception(exc)
    except InvalidStateError:
        pass


class SerialWriter:
    """Runs write jobs for one serial port, one at a time, in submission order.

    The writer owns the connection's default pacing. Changing ``pacing``
    affects jobs submitted afterwards.
    """

    def __init__(self, ser: Any, pacing: Pacing = NO_PACING) -> None:
        self.ser = ser
        self.pacing = pacing
        self._cond = threading.Condition()
        self._queue: deque[WriteJob] = deque()
        self._current: WriteJob | None = None
        self._pending_bytes: dict[str, int] = {}
        self._closed = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="serial-writer")
        self._thread.start()

    def submit(self, payload: bytes, pacing: Pacing | None = None, source: str = SOURCE_MCP) -> WriteJob:
        """Queue *payload* for writing.

        Raises:
            WriterClosed: The writer was stopped.
        """
        job = WriteJob(payload, self.pacing if pacing is None else pacing, source)
        with self._cond:
            if self._closed:
                raise WriterClosed("Connection is closed.")
            self._queue.append(job)
            self._pending_bytes[source] = self._pending_bytes.get(source, 0) + len(payload)
            self._cond.notify()
        return job

    def pending_bytes(self, source: str) -> int:
        """Bytes from *source* that are queued or being written."""
        with self._cond:
            return self._pending_bytes.get(source, 0)

    def discard_pending(self) -> None:
        """Fail every queued job and cancel the running one."""
        with self._cond:
            queued = self._drain_locked()
        for job in queued:
            _fail(job, WriteAborted("Write discarded by flush.", 0))

    def stop(self) -> None:
        """Refuse new jobs, cancel the running one, and fail the queued ones."""
        with self._cond:
            self._closed = True
            queued = self._drain_locked()
            self._cond.notify_all()
        for job in queued:
            _fail(job, WriterClosed("Connection closed before the write started."))

    def join(self) -> None:
        """Wait for the thread to exit; keep the handle and warn if it does not."""
        if self._thread is None:
            return
        self._thread.join(timeout=JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            logger.warning("Writer thread for %s did not stop within %.0f s.", self.ser.port, JOIN_TIMEOUT_S)
        else:
            self._thread = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _drain_locked(self) -> list[WriteJob]:
        """Remove and return the queued jobs and cancel the running one. Caller holds the lock."""
        queued = list(self._queue)
        self._queue.clear()
        for job in queued:
            self._release(job)
        if self._current is not None:
            self._current.cancel.set()
        return queued

    def _release(self, job: WriteJob) -> None:
        """Remove *job*'s bytes from the pending count. Caller holds the lock."""
        self._pending_bytes[job.source] -= len(job.payload)

    def _next_job(self) -> WriteJob | None:
        with self._cond:
            while not self._queue and not self._closed:
                self._cond.wait()
            if self._closed:
                return None
            job = self._queue.popleft()
            self._current = job
            return job

    def _run(self) -> None:
        while (job := self._next_job()) is not None:
            try:
                self._execute(job)
            finally:
                with self._cond:
                    self._current = None
                    self._release(job)

    def _execute(self, job: WriteJob) -> None:
        # Skip a job whose caller cancelled it while queued; writing it would
        # send bytes nobody wants, and set_result on it would raise.
        if not job.future.set_running_or_notify_cancel():
            return
        try:
            n = write_serial_payload(self.ser, job.payload, job.pacing, job.cancel)
            if not job.pacing.is_paced:
                self.ser.flush()
        except BaseException as exc:  # hand every failure to the waiting caller
            job.future.set_exception(exc)
        else:
            job.future.set_result(n)
