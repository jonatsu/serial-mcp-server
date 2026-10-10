"""``SerialBuffer``: the thread-safe byte buffer that holds received bytes until a tool reads them."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger("serial_mcp_server")


# ---------------------------------------------------------------------------
# SerialBuffer — thread-safe byte buffer
# ---------------------------------------------------------------------------


# Also the most a single read tool returns (``MAX_READ_BYTES``).
DEFAULT_BUFFER_BYTES = 1_048_576  # 1 MiB


class SerialBuffer:
    """Thread-safe byte buffer with blocking reads.

    The background reader writes into this buffer. MCP tool handlers
    read from it, and a read removes the bytes it returns.

    The buffer holds at most *max_size* bytes. A write that would pass the limit drops the oldest unread bytes
    and keeps the newest, so a device that sends faster than tools read loses its earlier output without an
    error. The writer never blocks and the serial reader is never slowed. The loss shows only in
    ``dropped_bytes`` (``serial.connection_status``) and as ``buffer_drop`` events in the port log.
    """

    def __init__(
        self, max_size: int = DEFAULT_BUFFER_BYTES, on_drop: Callable[[int], None] | None = None
    ) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._max_size = max_size
        self._on_drop = on_drop
        self._dropped_bytes = 0

    def write(self, data: bytes) -> None:
        """Append data to the buffer and wake any waiting readers.

        When the buffer exceeds its limit, the oldest bytes are dropped and counted, and
        ``on_drop`` is called with the count after the lock is released.
        """
        if not data:
            return
        excess = 0
        with self._cond:
            self._buf.extend(data)
            if len(self._buf) > self._max_size:
                excess = len(self._buf) - self._max_size
                del self._buf[:excess]
                self._dropped_bytes += excess
            self._cond.notify_all()
        if excess and self._on_drop is not None:
            self._on_drop(excess)

    def read(self, nbytes: int, timeout: float) -> bytes:
        """Read up to *nbytes*, blocking until data arrives or *timeout* expires.

        Returns as soon as any byte is buffered, so the result can be shorter than *nbytes*; it is empty
        only on timeout.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(timeout=remaining)
            n = min(nbytes, len(self._buf))
            result = bytes(self._buf[:n])
            del self._buf[:n]
            return result

    def read_until(self, delimiter: bytes, max_bytes: int, timeout: float) -> bytes:
        """Read until *delimiter* is found, *max_bytes* reached, or *timeout* expires.

        On a timeout or at *max_bytes* it returns whatever it has, without the delimiter, and removes those
        bytes, so a partial reply is not left for the next read.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                idx = self._buf.find(delimiter)
                if idx != -1:
                    end = min(idx + len(delimiter), max_bytes)
                    result = bytes(self._buf[:end])
                    del self._buf[:end]
                    return result
                if len(self._buf) >= max_bytes:
                    result = bytes(self._buf[:max_bytes])
                    del self._buf[:max_bytes]
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Return whatever we have.
                    n = min(len(self._buf), max_bytes)
                    result = bytes(self._buf[:n])
                    del self._buf[:n]
                    return result
                self._cond.wait(timeout=remaining)

    def clear(self) -> None:
        """Discard all buffered data. It is not counted in ``dropped_bytes``."""
        with self._lock:
            self._buf.clear()

    @property
    def dropped_bytes(self) -> int:
        """Bytes discarded because the buffer was full and nothing read them in time."""
        with self._lock:
            return self._dropped_bytes

    @property
    def available(self) -> int:
        """Number of bytes currently in the buffer."""
        with self._lock:
            return len(self._buf)
