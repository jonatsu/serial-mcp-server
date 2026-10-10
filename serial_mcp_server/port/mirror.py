"""PTY mirroring: ``MirrorSession`` tees received bytes to a pseudo-terminal (Unix only).

``create_reader`` picks a plain ``ReaderThread`` or a ``MirrorSession`` for a connection. It lives here, beside
the subclass it may build, so ``reader.py`` never imports the mirror.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from concurrent.futures import Future
from typing import Any

from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.reader import ReaderThread
from serial_mcp_server.port.writer import SOURCE_MIRROR, SerialWriter, WriterClosed
from serial_mcp_server.portlog.log import PortLogSlot

logger = logging.getLogger("serial_mcp_server")

_IS_UNIX = sys.platform != "win32"

# Input from the PTY (rw mode) that may wait in the writer: read from the PTY, not yet written to the port.
# At 115200 baud, 64 KiB is about six seconds of line time. Once the room left is under _PTY_READ_SIZE, the
# PTY master leaves the select set. Nothing is dropped: the rest waits in the kernel's PTY buffer, and the
# program writing to the slave blocks once that buffer is full, so a large paste is slowed to the port's
# speed.
MIRROR_BACKLOG_LIMIT = 64 * 1024
# Bytes per ``os.read`` of the PTY master. Each read becomes one writer job, so a paste longer than this is
# queued as several jobs and an MCP write submitted in between can run between them. The master is polled only
# while at least this much room is left, which keeps the backlog at or under MIRROR_BACKLOG_LIMIT.
_PTY_READ_SIZE = 4096

# sys.platform rather than _IS_UNIX, so type checkers see these imports bound on Unix.
if sys.platform != "win32":
    import fcntl
    import select
    import termios
    import tty


# ---------------------------------------------------------------------------
# MirrorSession — PTY tee on top of ReaderThread (Unix only)
# ---------------------------------------------------------------------------


class MirrorSession(ReaderThread):
    """Background reader that also tees serial data to a PTY.

    Each received chunk goes to the read buffer, the port log, and the subscribers first, then to the PTY
    master without blocking. That copy is best effort: when no program reads the slave and the PTY buffer is
    full, it is dropped, and the return value of the write is ignored, so a chunk the PTY accepts only in part
    loses its tail. The buffer and the log are unaffected.

    In rw mode, data written to the PTY slave by an external tool is forwarded to the real serial port through
    the writer, with the connection's pacing at that moment. The forward does not check for a running script,
    so an operator can still send bytes to the device mid-run. Input past ``MIRROR_BACKLOG_LIMIT`` waits in
    the kernel and is not lost. A forward that fails is only logged and is not retried
    (``_log_forward_failure``). In ro mode the master is never read: input typed into the slave is neither
    sent nor discarded, and it stays in the PTY buffer until that buffer is full.
    """

    def __init__(
        self,
        ser: Any,
        buffer: SerialBuffer,
        mode: str,
        writer: SerialWriter,
        log_slot: PortLogSlot | None = None,
        link_path: str | None = None,
    ) -> None:
        super().__init__(ser, buffer, log_slot)
        self.writer = writer
        self.mode = mode  # "ro" or "rw"
        self.link_path = link_path

        # Create PTY pair.
        self._master_fd, self._slave_fd = os.openpty()

        # Non-blocking master so writes don't stall the reader thread
        # when nothing is connected to the slave.
        flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Put slave in raw mode and set the speed it reports. A rate with no termios constant reports 115200.
        try:
            tty.setraw(self._slave_fd)
            attrs = termios.tcgetattr(self._slave_fd)
            baud = getattr(termios, f"B{ser.baudrate}", termios.B115200)
            attrs[4] = baud  # ispeed
            attrs[5] = baud  # ospeed
            termios.tcsetattr(self._slave_fd, termios.TCSANOW, attrs)
        except Exception:
            pass  # Best-effort baud config.

        self.pty_slave_path: str = os.ttyname(self._slave_fd)
        self._mirror_index: int | None = None  # Set by create_reader if link is used.

        # Create symlink if requested. Whatever file or link already has that name is replaced; a directory
        # is not, and the failure is only logged.
        self._link_created = False
        if self.link_path:
            try:
                if os.path.islink(self.link_path) or os.path.exists(self.link_path):
                    os.unlink(self.link_path)
                os.symlink(self.pty_slave_path, self.link_path)
                self._link_created = True
            except OSError as exc:
                logger.warning("Failed to create mirror symlink %s: %s", self.link_path, exc)

    def join(self) -> None:
        """Join the thread, then release the PTY, symlink, and mirror index, even if the join fails."""
        try:
            super().join()
        finally:
            self._release_resources()

    def _release_resources(self) -> None:
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        try:
            os.close(self._slave_fd)
        except OSError:
            pass
        if self._link_created and self.link_path:
            try:
                os.unlink(self.link_path)
                self._link_created = False
            except OSError:
                pass
        if self._mirror_index is not None:
            _release_mirror_index(self._mirror_index)
            self._mirror_index = None

    def _run(self) -> None:
        ser_fd = self.ser.fileno()
        select_errors = 0
        serial_errors = 0

        while not self._stop.is_set():
            try:
                readable, _, _ = select.select(self._read_fds(ser_fd), [], [], 0.05)
            except (OSError, ValueError):
                if self._stop.is_set():
                    break
                select_errors += 1
                if select_errors >= self._MAX_CONSECUTIVE_ERRORS:
                    logger.warning(
                        "Mirror reader for %s stopping after %d consecutive select errors.",
                        self.ser.port,
                        select_errors,
                    )
                    self._close_subscribers()
                    return
                time.sleep(0.1)
                continue
            select_errors = 0

            for fd in readable:
                if fd == ser_fd:
                    try:
                        waiting = self.ser.in_waiting
                        data = self.ser.read(waiting or 1)
                        if data:
                            self._on_data(data)
                    except Exception:
                        if self._stop.is_set():
                            return
                        serial_errors += 1
                        if serial_errors >= self._MAX_CONSECUTIVE_ERRORS:
                            logger.warning(
                                "Mirror reader for %s stopping after %d consecutive serial errors.",
                                self.ser.port,
                                serial_errors,
                            )
                            self._close_subscribers()
                            return
                        time.sleep(0.1)
                    else:
                        serial_errors = 0

                elif fd == self._master_fd:
                    try:
                        pty_data = os.read(self._master_fd, _PTY_READ_SIZE)
                    except OSError:
                        continue  # PTY client disconnected.
                    if pty_data and not self._forward(pty_data):
                        return

    def _read_fds(self, ser_fd: int) -> list[int]:
        """File descriptors to wait on; the PTY master only while the writer has room."""
        if self.mode != "rw":
            return [ser_fd]
        room = MIRROR_BACKLOG_LIMIT - self.writer.pending_bytes(SOURCE_MIRROR)
        return [ser_fd, self._master_fd] if room >= _PTY_READ_SIZE else [ser_fd]

    def _forward(self, data: bytes) -> bool:
        """Queue PTY input for the port. Return False once the writer is closed."""
        try:
            job = self.writer.submit(data, source=SOURCE_MIRROR)
        except WriterClosed:
            return False
        job.future.add_done_callback(_log_forward_failure)
        return True

    def _on_data(self, data: bytes) -> None:
        """Write to both the buffer and the PTY master."""
        self._deliver(data)
        try:
            os.write(self._master_fd, data)
        except (OSError, BlockingIOError):
            pass  # PTY buffer full or client not connected — drop mirror data.

    def mirror_info(self) -> dict[str, Any] | None:
        return {
            "pty_path": self.pty_slave_path,
            "link": self.link_path,
            "mode": self.mode,
        }


def _log_forward_failure(future: Future[int]) -> None:
    """Warn about a failed forward. Its input is lost, not retried.

    A write timeout, or a ``serial.flush`` of output that discards the queue, fails the job. A closed writer
    is expected at shutdown, so it does not warn.
    """
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None and not isinstance(exc, WriterClosed):
        logger.warning("Forwarding mirror input to the serial port failed: %s", exc)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_mirror_indices_lock = threading.Lock()
_mirror_indices_in_use: set[int] = set()


def _acquire_mirror_index() -> int:
    """Return the lowest available mirror index.

    Only this process tracks the indices. A second server using the same link base gets the same numbers, so
    its link replaces the first one's, and either server removes the link when its connection closes.
    """
    with _mirror_indices_lock:
        idx = 0
        while idx in _mirror_indices_in_use:
            idx += 1
        _mirror_indices_in_use.add(idx)
        return idx


def _release_mirror_index(idx: int) -> None:
    """Return a mirror index to the pool."""
    with _mirror_indices_lock:
        _mirror_indices_in_use.discard(idx)


def create_reader(
    ser: Any,
    buffer: SerialBuffer,
    mirror_mode: str,
    mirror_link_base: str | None,
    *,
    writer: SerialWriter,
    log_slot: PortLogSlot | None = None,
) -> ReaderThread:
    """Create the appropriate reader for a serial connection.

    *mirror_mode*: ``"off"``, ``"ro"``, or ``"rw"``. In ``rw`` mode the mirror
    forwards PTY input through *writer*. A mirror needs a Unix platform; on Windows this returns a plain
    reader without a warning (``config.load`` already turns the mode off with one).
    """
    if mirror_mode == "off" or not _IS_UNIX:
        return ReaderThread(ser, buffer, log_slot)

    link_path: str | None = None
    mirror_index: int | None = None
    if mirror_link_base:
        mirror_index = _acquire_mirror_index()
        link_path = f"{mirror_link_base}{mirror_index}"

    session = MirrorSession(
        ser, buffer, mode=mirror_mode, writer=writer, link_path=link_path, log_slot=log_slot
    )
    session._mirror_index = mirror_index
    return session
