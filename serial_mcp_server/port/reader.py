"""``ReaderThread``: the background thread that reads the port into its buffer and feeds subscribers."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.portlog.log import PortLogSlot

logger = logging.getLogger("serial_mcp_server")

# pyserial read timeout of every port the server opens: how long the reader blocks for one byte before it checks for
# a stop request. Fixed rather than taken from ``serial.open``'s ``timeout_ms``: pyserial treats 0 as non-blocking,
# which would make the reader spin, and a long timeout would delay stopping it. ``timeout_ms`` sets only the read
# tools' default wait, so code that reads the port object directly waits at most this long.
READ_POLL_MS = 200


# ---------------------------------------------------------------------------
# ReaderThread — background serial reader (cross-platform)
# ---------------------------------------------------------------------------


class _ReaderSubscriber(Protocol):
    def feed(self, data: bytes) -> None: ...

    def close(self, error: Exception | None = None) -> None: ...


class ReaderThread:
    """Background thread that continuously reads from a serial port into a buffer.

    A read takes whatever ``in_waiting`` reports. When nothing is waiting it reads exactly one byte, blocking
    up to the port's read timeout (``READ_POLL_MS``). The first byte after a quiet period is therefore a chunk
    of its own, which shows as a one-byte ``rx`` entry in the jsonl port log. Each
    chunk is logged, then buffered, then fed to the subscribers.
    """

    def __init__(self, ser: Any, buffer: SerialBuffer, log_slot: PortLogSlot | None = None) -> None:
        self.ser = ser
        self.buffer = buffer
        self.log_slot = log_slot if log_slot is not None else PortLogSlot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._subscribers: tuple[_ReaderSubscriber, ...] = ()
        self._subscribers_lock = threading.Lock()
        self._subscriber_stream_ended = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="serial-reader")
        self._thread.start()

    def signal_stop(self) -> None:
        """Ask the thread to stop without waiting for it."""
        self._stop.set()

    def join(self) -> None:
        """Wait up to 3 s for the thread to exit. Call after ``signal_stop``.

        A thread still blocked after that, such as in a stuck driver call, is not waited for further. The handle
        is dropped either way, so ``alive`` reports False afterwards.
        """
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def stop(self) -> None:
        self.signal_stop()
        self.join()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def subscribe(self, subscriber: _ReaderSubscriber) -> Callable[[], None]:
        """Subscribe to received chunks and return a function that removes the subscriber."""
        with self._subscribers_lock:
            if self._subscriber_stream_ended:
                close_now = True
            else:
                self._subscribers = (*self._subscribers, subscriber)
                close_now = False
        if close_now:
            self._close_subscriber(subscriber)

        def unsubscribe() -> None:
            self._remove_subscriber(subscriber)

        return unsubscribe

    def _remove_subscriber(self, subscriber: _ReaderSubscriber) -> None:
        with self._subscribers_lock:
            self._subscribers = tuple(current for current in self._subscribers if current is not subscriber)

    def _close_subscriber(self, subscriber: _ReaderSubscriber, error: Exception | None = None) -> None:
        try:
            if error is None:
                subscriber.close()
            else:
                subscriber.close(error)
        except Exception as exc:
            logger.warning("Reader subscriber close for %s failed: %s", self.ser.port, exc, exc_info=True)

    def _close_subscribers(self) -> None:
        with self._subscribers_lock:
            self._subscriber_stream_ended = True
            subscribers = self._subscribers
            self._subscribers = ()
        for subscriber in subscribers:
            self._close_subscriber(subscriber)

    # Failed reads in a row, 0.1 s apart (about one second), after which the thread gives up for good. It
    # closes its subscribers and logs a warning; the connection stays registered, nothing else marks it as
    # failed, and reads return only what was already buffered.
    _MAX_CONSECUTIVE_ERRORS = 10

    def _run(self) -> None:
        errors = 0
        while not self._stop.is_set():
            try:
                waiting = self.ser.in_waiting
                if waiting:
                    data = self.ser.read(waiting)
                    if data:
                        self._on_data(data)
                else:
                    # Block for up to 1 byte with the serial port's own timeout.
                    data = self.ser.read(1)
                    if data:
                        self._on_data(data)
                errors = 0
            except Exception:
                if self._stop.is_set():
                    break
                errors += 1
                if errors >= self._MAX_CONSECUTIVE_ERRORS:
                    logger.warning(
                        "Reader thread for %s stopping after %d consecutive errors.", self.ser.port, errors
                    )
                    self._close_subscribers()

                    break
                time.sleep(0.1)

    def _deliver(self, data: bytes) -> None:
        """Record each chunk before delivering it to the current subscribers."""
        with self._subscribers_lock:
            subscribers = self._subscribers
        self.log_slot.rx(data)
        self.buffer.write(data)
        for subscriber in subscribers:
            try:
                subscriber.feed(data)
            except Exception as exc:
                logger.warning(
                    "Reader subscriber for %s failed and was removed: %s",
                    self.ser.port,
                    exc,
                    exc_info=True,
                )
                self._remove_subscriber(subscriber)
                self._close_subscriber(subscriber, exc)

    def _on_data(self, data: bytes) -> None:
        """Called when data arrives from the serial port. Override to tee; call ``_deliver`` once."""
        self._deliver(data)

    def mirror_info(self) -> dict[str, Any] | None:
        """Return mirror metadata, or None if this is a plain reader."""
        return None
