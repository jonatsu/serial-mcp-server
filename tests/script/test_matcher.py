"""Behavioral tests for matcher windows and reader subscribers."""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable

import pytest

from serial_mcp_server.mcp.tools.io import handle_read
from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.mirror import MirrorSession
from serial_mcp_server.port.reader import ReaderThread
from serial_mcp_server.script.matcher import OutputMatcher


class _ChunkedSerial:
    port: str = "test-port"

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks: deque[bytes] = deque(chunks)
        self._lock: threading.Lock = threading.Lock()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._chunks[0]) if self._chunks else 0

    def read(self, nbytes: int) -> bytes:
        with self._lock:
            if not self._chunks:
                return b""
            chunk = self._chunks.popleft()
            if len(chunk) > nbytes:
                self._chunks.appendleft(chunk[nbytes:])
            return chunk[:nbytes]


class _RecordingSubscriber:
    def __init__(self, expected_chunks: int | None = None, failure: Exception | None = None) -> None:
        self.expected_chunks: int | None = expected_chunks
        self.failure: Exception | None = failure
        self.feed_calls: int = 0
        self.chunks: list[bytes] = []
        self.received: threading.Event = threading.Event()
        self.closed: threading.Event = threading.Event()
        self.close_error: Exception | None = None

    def feed(self, data: bytes) -> None:
        self.feed_calls += 1
        if self.failure is not None:
            raise self.failure
        self.chunks.append(data)
        if self.expected_chunks is not None and len(self.chunks) >= self.expected_chunks:
            self.received.set()

    def close(self, error: Exception | None = None) -> None:
        self.close_error = error
        self.closed.set()


def _start_waiter(
    matcher: OutputMatcher,
) -> tuple[threading.Thread, threading.Event, list[bool]]:
    started = threading.Event()
    finished = threading.Event()
    outcomes: list[bool] = []

    def wait_for_output() -> None:
        started.set()
        outcomes.append(matcher.wait(time.monotonic() + 1.0))
        finished.set()

    thread = threading.Thread(target=wait_for_output, daemon=True)
    thread.start()
    assert started.wait(timeout=1.0)
    return thread, finished, outcomes


def test_consuming_a_match_requires_another_occurrence() -> None:
    matcher = OutputMatcher("utf-8")
    matcher.feed(b"ready ready")

    first = matcher.snapshot()
    first_match = re.search("ready", first.text)
    assert first_match is not None
    assert matcher.consume(first, first_match.end())

    second = matcher.snapshot()
    second_match = re.search("ready", second.text)
    assert second_match is not None
    assert matcher.consume(second, second_match.end())
    assert matcher.snapshot().text == ""


def test_a_stale_snapshot_cannot_consume_text_after_window_overflow() -> None:
    matcher = OutputMatcher("utf-8")
    matcher.feed(b"before")
    stale = matcher.snapshot()

    matcher.feed(b"x" * 1_048_577)

    assert not matcher.consume(stale, len("before"))
    assert matcher.snapshot().text == "x" * 1_048_576
    assert matcher.dropped_chars == len("before") + 1


def test_fail_pattern_view_keeps_overlap_across_feed_calls() -> None:
    matcher = OutputMatcher("utf-8")
    matcher.feed(b"x" * 300 + b"panic e")
    previous = matcher.take_since_fail_check()
    assert previous.endswith("panic e")

    matcher.feed(b"rror")
    text = matcher.take_since_fail_check()

    assert "panic error" in text
    assert len(text) == 260


def test_split_utf8_codepoint_decodes_incrementally() -> None:
    matcher = OutputMatcher("utf-8")
    matcher.feed(b"\xe2\x82")

    assert matcher.snapshot().text == ""

    matcher.feed(b"\xac")

    assert matcher.snapshot().text == "€"


def test_window_overflow_keeps_the_limit_and_exact_drop_count() -> None:
    matcher = OutputMatcher("utf-8")

    matcher.feed(b"a" * (1_048_576 + 17))

    assert len(matcher.snapshot().text) == 1_048_576
    assert matcher.dropped_chars == 17


def test_discard_clears_match_text_but_keeps_output_for_fail_checks() -> None:
    matcher = OutputMatcher("utf-8")
    matcher.feed(b"panic")

    matcher.discard()

    assert matcher.snapshot().text == ""
    assert "panic" in matcher.take_since_fail_check()


def test_closing_flushes_an_incomplete_encoded_character() -> None:
    matcher = OutputMatcher("utf-8")
    matcher.feed(b"\xe2\x82")

    matcher.close()

    assert matcher.snapshot().text == "\ufffd"
    assert matcher.closed
    assert matcher.error is None


def test_wait_returns_when_data_arrives_after_the_snapshot() -> None:
    matcher = OutputMatcher("utf-8")
    _ = matcher.snapshot()
    thread, finished, outcomes = _start_waiter(matcher)

    matcher.feed(b"ready")

    assert finished.wait(timeout=1.0)
    thread.join(timeout=1.0)
    assert outcomes == [True]
    assert matcher.snapshot().text == "ready"


def test_wait_returns_when_the_matcher_closes() -> None:
    matcher = OutputMatcher("utf-8")
    _ = matcher.snapshot()
    thread, finished, outcomes = _start_waiter(matcher)

    matcher.close()

    assert finished.wait(timeout=1.0)
    thread.join(timeout=1.0)
    assert outcomes == [True]
    assert matcher.closed


def test_wait_returns_when_interrupted_without_data() -> None:
    matcher = OutputMatcher("utf-8")
    _ = matcher.snapshot()
    thread, finished, outcomes = _start_waiter(matcher)

    matcher.interrupt()

    assert finished.wait(timeout=1.0)
    thread.join(timeout=1.0)
    assert outcomes == [True]
    assert matcher.snapshot().text == ""


def test_wait_returns_false_when_its_deadline_has_passed() -> None:
    matcher = OutputMatcher("utf-8")
    _ = matcher.snapshot()

    assert not matcher.wait(time.monotonic() - 1.0)


def test_subscriber_failure_does_not_stop_reader_or_steal_serial_read_bytes() -> None:
    data = [b"first", b"second"]
    serial = _ChunkedSerial(data)
    buffer = SerialBuffer()
    reader = ReaderThread(serial, buffer)
    broken = _RecordingSubscriber(failure=ValueError("subscriber failed"))
    healthy = _RecordingSubscriber(expected_chunks=2)
    unsubscribe_broken = reader.subscribe(broken)
    unsubscribe_healthy = reader.subscribe(healthy)
    reader.start()

    try:
        assert healthy.received.wait(timeout=1.0)
    finally:
        reader.stop()
        unsubscribe_broken()
        unsubscribe_healthy()

    assert broken.feed_calls == 1
    assert broken.closed.is_set()
    assert broken.close_error is broken.failure
    assert healthy.chunks == data
    assert buffer.read(100, timeout=0.01) == b"firstsecond"


def test_repeated_reader_errors_close_current_and_late_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedSerial:
        port: str = "lost-device"

        @property
        def in_waiting(self) -> int:
            raise OSError("device unavailable")

    def skip_retry_delay(_seconds: float) -> None:
        pass

    monkeypatch.setattr(time, "sleep", skip_retry_delay)
    monkeypatch.setattr(ReaderThread, "_MAX_CONSECUTIVE_ERRORS", 2)
    reader = ReaderThread(FailedSerial(), SerialBuffer())
    matcher = OutputMatcher("utf-8")
    unsubscribe_matcher = reader.subscribe(matcher)
    _ = matcher.snapshot()
    thread, finished, outcomes = _start_waiter(matcher)
    reader.start()

    try:
        assert finished.wait(timeout=1.0)
    finally:
        reader.stop()
        unsubscribe_matcher()

    late_subscriber = _RecordingSubscriber()
    unsubscribe_late = reader.subscribe(late_subscriber)
    thread.join(timeout=1.0)
    unsubscribe_late()

    assert outcomes == [True]
    assert matcher.closed
    assert late_subscriber.closed.is_set()


def test_subscribe_and_unsubscribe_do_not_block_reader_callbacks() -> None:
    reader = ReaderThread(object(), SerialBuffer())
    entered = threading.Event()
    release = threading.Event()
    delivered = threading.Event()
    persistent = _RecordingSubscriber()
    added = _RecordingSubscriber()
    added_unsubscriber: list[Callable[[], None]] = []

    class PausedSubscriber(_RecordingSubscriber):
        def feed(self, data: bytes) -> None:
            self.chunks.append(data)
            entered.set()
            assert release.wait(timeout=1.0)

    paused = PausedSubscriber()
    unsubscribe_paused = reader.subscribe(paused)
    unsubscribe_persistent = reader.subscribe(persistent)

    def deliver_first_chunk() -> None:
        reader._deliver(b"first")
        delivered.set()

    delivery_thread = threading.Thread(target=deliver_first_chunk, daemon=True)
    delivery_thread.start()
    assert entered.wait(timeout=1.0)

    subscribed = threading.Event()

    def update_subscribers() -> None:
        unsubscribe_paused()
        added_unsubscriber.append(reader.subscribe(added))
        subscribed.set()

    update_thread = threading.Thread(target=update_subscribers, daemon=True)
    update_thread.start()
    try:
        assert subscribed.wait(timeout=1.0)
    finally:
        release.set()

    assert delivered.wait(timeout=1.0)
    delivery_thread.join(timeout=1.0)
    update_thread.join(timeout=1.0)
    reader._deliver(b"second")
    added_unsubscriber[0]()
    unsubscribe_persistent()

    assert paused.chunks == [b"first"]
    assert persistent.chunks == [b"first", b"second"]
    assert added.chunks == [b"second"]


@pytest.mark.skipif(sys.platform == "win32", reason="PTY mirror requires Unix")
async def test_mirror_subscriber_and_serial_read_receive_the_same_bytes(connected_entry) -> None:
    state, conn = connected_entry
    mirror_session = await asyncio.to_thread(
        MirrorSession, conn.ser, conn.buffer, mode="ro", writer=conn.writer
    )
    matcher = OutputMatcher(conn.encoding)
    unsubscribe_matcher = mirror_session.subscribe(matcher)
    conn.reader = mirror_session
    received = b"device output\n"

    try:
        await asyncio.to_thread(mirror_session._on_data, received)
        result = await handle_read(
            state, {"connection_id": conn.connection_id, "nbytes": len(received), "timeout_ms": 0}
        )
    finally:
        unsubscribe_matcher()
        await asyncio.to_thread(mirror_session.stop)

    assert result["ok"]
    assert result["data"] == received.decode(conn.encoding)
    assert matcher.snapshot().text == result["data"]
