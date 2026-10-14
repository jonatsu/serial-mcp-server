"""Tests for serial_mcp_server.port.mirror — MirrorSession, create_reader, and PTY forwarding."""

from __future__ import annotations

import errno
import os
import select
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.mirror import MIRROR_BACKLOG_LIMIT, MirrorSession, create_reader
from serial_mcp_server.port.pacing import Pacing
from serial_mcp_server.port.reader import ReaderThread
from serial_mcp_server.port.writer import SerialWriter
from serial_mcp_server.script.matcher import OutputMatcher

# The PTY mirror itself is Unix-only.
_IS_UNIX = sys.platform != "win32"


@pytest.mark.skipif(not _IS_UNIX, reason="PTY mirror requires Unix")
class TestMirrorSession:
    def test_creates_pty(self):
        ser = MagicMock()
        ser.baudrate = 115200
        buf = SerialBuffer()
        mirror = MirrorSession(ser, buf, writer=SerialWriter(ser), mode="ro")
        try:
            assert mirror.pty_slave_path
            assert os.path.exists(mirror.pty_slave_path)
            assert mirror.mode == "ro"
        finally:
            mirror.stop()

    def test_mirror_info(self):
        ser = MagicMock()
        ser.baudrate = 115200
        buf = SerialBuffer()
        mirror = MirrorSession(ser, buf, writer=SerialWriter(ser), mode="rw")
        try:
            info = mirror.mirror_info()
            assert info is not None
            assert "pty_path" in info
            assert info["mode"] == "rw"
            assert info["link"] is None
        finally:
            mirror.stop()

    def test_symlink_creation(self, tmp_path):
        link = str(tmp_path / "testlink")
        ser = MagicMock()
        ser.baudrate = 115200
        buf = SerialBuffer()
        mirror = MirrorSession(ser, buf, writer=SerialWriter(ser), mode="ro", link_path=link)
        try:
            assert os.path.islink(link)
            assert os.readlink(link) == mirror.pty_slave_path
        finally:
            mirror.stop()
        # Symlink cleaned up on stop
        assert not os.path.exists(link)

    def test_data_to_buffer_and_pty(self):
        ser = MagicMock()
        ser.baudrate = 115200
        buf = SerialBuffer()
        mirror = MirrorSession(ser, buf, writer=SerialWriter(ser), mode="ro")
        try:
            # Simulate data arriving via _on_data
            mirror._on_data(b"test data")
            assert buf.read(100, timeout=0.01) == b"test data"
        finally:
            mirror.stop()

    def test_stop_cleans_up(self):
        ser = MagicMock()
        ser.baudrate = 115200
        buf = SerialBuffer()
        mirror = MirrorSession(ser, buf, writer=SerialWriter(ser), mode="ro")
        mirror.stop()
        # PTY fds are closed, no error expected
        assert not mirror.alive

    def test_repeated_serial_errors_close_subscribers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"data")

        class FailedPort:
            port = "mirror-test-port"
            baudrate = 115200

            def fileno(self) -> int:
                return read_fd

            @property
            def in_waiting(self) -> int:
                raise OSError("device unavailable")

        monkeypatch.setattr(ReaderThread, "_MAX_CONSECUTIVE_ERRORS", 2)
        port = FailedPort()
        mirror = MirrorSession(port, SerialBuffer(), writer=SerialWriter(port), mode="ro")
        matcher = OutputMatcher("utf-8")
        mirror.subscribe(matcher)
        _ = matcher.snapshot()
        mirror.start()

        try:
            stream_closed = matcher.wait(time.monotonic() + 1.0)
        finally:
            mirror.stop()
            os.close(read_fd)
            os.close(write_fd)

        assert stream_closed
        assert matcher.closed


# ---------------------------------------------------------------------------
# create_reader factory
# ---------------------------------------------------------------------------


class TestCreateReader:
    def test_off_returns_reader_thread(self):
        ser = MagicMock()
        buf = SerialBuffer()
        reader = create_reader(ser, buf, "off", None, writer=SerialWriter(ser))
        assert isinstance(reader, ReaderThread)
        assert not isinstance(reader, MirrorSession) if _IS_UNIX else True

    @pytest.mark.skipif(not _IS_UNIX, reason="PTY mirror requires Unix")
    def test_ro_returns_mirror_session(self):
        ser = MagicMock()
        ser.baudrate = 115200
        buf = SerialBuffer()
        reader = create_reader(ser, buf, "ro", None, writer=SerialWriter(ser))
        try:
            assert isinstance(reader, MirrorSession)
            assert reader.mode == "ro"
        finally:
            reader.stop()

    @pytest.mark.skipif(not _IS_UNIX, reason="PTY mirror requires Unix")
    def test_rw_returns_mirror_session(self):
        ser = MagicMock()
        ser.baudrate = 115200
        buf = SerialBuffer()
        reader = create_reader(ser, buf, "rw", None, writer=SerialWriter(ser))
        try:
            assert isinstance(reader, MirrorSession)
            assert reader.mode == "rw"
        finally:
            reader.stop()

    @pytest.mark.skipif(not _IS_UNIX, reason="PTY mirror requires Unix")
    def test_link_base_appends_index(self, tmp_path):
        ser = MagicMock()
        ser.baudrate = 115200
        buf = SerialBuffer()
        link_base = str(tmp_path / "ttyMCP")
        reader = create_reader(ser, buf, "ro", link_base, writer=SerialWriter(ser))
        try:
            info = reader.mirror_info()
            assert info is not None
            assert info["link"].startswith(link_base)
        finally:
            reader.stop()

    def test_non_unix_always_returns_reader_thread(self):
        ser = MagicMock()
        buf = SerialBuffer()
        with patch("serial_mcp_server.port.mirror._IS_UNIX", False):
            reader = create_reader(ser, buf, "rw", None, writer=SerialWriter(ser))
        assert type(reader) is ReaderThread


# ---------------------------------------------------------------------------
# Mirror input goes through the writer thread (Unix only)
# ---------------------------------------------------------------------------

WAIT_S = 2.0


class _PipePort:
    """Fake port: ``fileno`` and ``read`` use a pipe; the first ``write`` can be held."""

    baudrate = 115200
    port = "pipe-test-port"
    in_waiting = 0

    def __init__(self, hold_first_write: bool = False) -> None:
        self._rx_read, self.rx_write = os.pipe()
        self.writes: list[bytes] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._hold = hold_first_write

    def fileno(self) -> int:
        return self._rx_read

    def read(self, n: int) -> bytes:
        # Like a port with a read timeout, so a plain ReaderThread can notice a stop request.
        ready, _, _ = select.select([self._rx_read], [], [], 0.05)
        return os.read(self._rx_read, n) if ready else b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self._hold:
            self._hold = False
            self.entered.set()
            assert self.release.wait(WAIT_S)
        return len(data)

    def flush(self) -> None:
        pass

    def close_pipe(self) -> None:
        os.close(self._rx_read)
        os.close(self.rx_write)


@pytest.fixture()
def rw_session():
    """Factory for started rw ``MirrorSession`` objects; tears everything down."""
    made = []

    def make(port: _PipePort, pacing: Pacing | None = None):
        buf = SerialBuffer()
        writer = SerialWriter(port, pacing or Pacing())
        session = MirrorSession(port, buf, writer=writer, mode="rw")
        writer.start()
        session.start()
        made.append((port, writer, session))
        return buf, writer, session

    yield make
    for port, writer, session in made:
        port.release.set()
        writer.stop()
        session.signal_stop()
        writer.join()
        session.join()
        port.close_pipe()


@pytest.mark.skipif(not _IS_UNIX, reason="PTY mirror requires Unix")
class TestMirrorForwarding:
    def test_failed_forward_warns_without_stopping_reception(self, rw_session, caplog):
        class FailedWritePort(_PipePort):
            def write(self, data: bytes) -> int:
                raise OSError("forward failed")

        port = FailedWritePort()
        buf, _writer, session = rw_session(port)
        os.write(session._slave_fd, b"operator input")
        deadline = time.monotonic() + WAIT_S
        while not caplog.records and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        warnings = [record for record in caplog.records if record.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "Forwarding mirror input" in warnings[0].message
        os.write(port.rx_write, b"still receiving")
        assert _read_buffer(buf, len(b"still receiving")) == b"still receiving"
        assert session.alive

    def test_received_data_flows_while_a_paced_write_is_held(self, rw_session):
        port = _PipePort(hold_first_write=True)
        buf, _writer, session = rw_session(port, Pacing(chunk_size=1, chunk_delay_ms=8))

        os.write(session._slave_fd, b"paste")
        assert port.entered.wait(WAIT_S)

        os.write(port.rx_write, b"device output")
        received = b""
        deadline = time.monotonic() + WAIT_S
        while len(received) < len(b"device output") and time.monotonic() < deadline:
            received += buf.read(100, timeout=0.05)
        assert received == b"device output"

    def test_pty_input_reaches_the_port_in_order(self, rw_session):
        port = _PipePort()
        _buf, writer, session = rw_session(port)

        os.write(session._slave_fd, b"ABCDE")
        deadline = time.monotonic() + WAIT_S
        while sum(map(len, port.writes)) < 5 and time.monotonic() < deadline:
            time.sleep(0.005)

        assert b"".join(port.writes) == b"ABCDE"

    def test_forwards_using_the_connection_pacing(self, rw_session):
        port = _PipePort()
        _buf, _writer, session = rw_session(port, Pacing(chunk_size=2, chunk_delay_ms=1))

        os.write(session._slave_fd, b"ABCDE")
        deadline = time.monotonic() + WAIT_S
        while sum(map(len, port.writes)) < 5 and time.monotonic() < deadline:
            time.sleep(0.005)

        assert port.writes == [b"AB", b"CD", b"E"]

    def test_large_paste_stays_within_the_backlog_bound_and_arrives_whole(self, rw_session):
        port = _PipePort(hold_first_write=True)
        _buf, writer, session = rw_session(port)
        paste = bytes(range(256)) * 800  # 200 KiB, more than the bound
        # os.write may be partial on a full PTY, so loop in the thread.
        pasted = threading.Thread(target=_write_all, args=(session._slave_fd, paste), daemon=True)
        pasted.start()

        assert port.entered.wait(WAIT_S)
        peak = 0
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            peak = max(peak, writer.pending_bytes("mirror"))
            time.sleep(0.005)
        assert 0 < peak <= MIRROR_BACKLOG_LIMIT

        port.release.set()
        pasted.join(WAIT_S * 5)
        deadline = time.monotonic() + WAIT_S * 5
        while sum(map(len, port.writes)) < len(paste) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert b"".join(port.writes) == paste

    def test_session_exits_when_the_writer_closes(self, rw_session):
        port = _PipePort()
        _buf, writer, session = rw_session(port)

        writer.stop()
        os.write(session._slave_fd, b"late")
        session._thread.join(WAIT_S)

        assert not session.alive


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _read_pty(fd: int, count: int) -> bytes:
    received = bytearray()
    deadline = time.monotonic() + WAIT_S
    while len(received) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        received.extend(os.read(fd, count - len(received)))
    return bytes(received)


@pytest.mark.skipif(not _IS_UNIX, reason="PTY mirror requires Unix")
class TestMirrorOutputReliability:
    def test_real_pty_backpressure_preserves_a_burst_with_trailing_newlines(self, rw_session):
        port = _PipePort()
        buf, _writer, session = rw_session(port)
        payload = b"x" * 100_000 + b"\r\nlast line\r\n"
        session._on_data(payload)

        assert session.mirror_info()["pending_bytes"] > 0
        assert _read_buffer(buf, len(payload)) == payload
        assert _read_pty(session._slave_fd, len(payload)) == payload

    def test_short_writes_retain_the_complete_ordered_stream(self, rw_session, monkeypatch):
        port = _PipePort()
        buf, _writer, session = rw_session(port)
        original_write = os.write

        def short_write(fd, data):
            return original_write(fd, data[:3] if fd == session._master_fd else data)

        monkeypatch.setattr("serial_mcp_server.port.mirror.os.write", short_write)
        payload = b"long command " * 30 + b"\r\nnext\x1b[2K\rline\n"
        session._on_data(payload)

        assert _read_pty(session._slave_fd, len(payload)) == payload
        assert buf.read(len(payload), timeout=0.05) == payload

    def test_split_newlines_and_terminal_sequences_are_transparent(self, rw_session):
        port = _PipePort()
        buf, _writer, session = rw_session(port)
        chunks = [b"command " * 40 + b"\r", b"\nnext\r", b"replace\n\x1b[2K\x1b[1Gend"]
        payload = b"".join(chunks)
        for chunk in chunks:
            os.write(port.rx_write, chunk)
            assert _read_buffer(buf, len(chunk)) == chunk

        assert _read_pty(session._slave_fd, len(payload)) == payload
        _write_all(session._slave_fd, payload)
        deadline = time.monotonic() + WAIT_S
        while len(b"".join(port.writes)) < len(payload) and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        assert b"".join(port.writes) == payload

    def test_overflow_fails_only_mirror_and_releases_its_endpoints(self, rw_session, monkeypatch, caplog):
        port = _PipePort()
        buf, _writer, session = rw_session(port)
        master, slave = session._master_fd, session._slave_fd
        viewer = os.open(session.pty_slave_path, os.O_RDWR | os.O_NOCTTY)
        original_write = os.write

        def stalled_write(fd, data):
            if fd == master:
                raise BlockingIOError()
            return original_write(fd, data)

        monkeypatch.setattr("serial_mcp_server.port.mirror.os.write", stalled_write)
        payload = b"x" * (1024 * 1024)
        session._on_data(payload)
        assert session.mirror_info()["pending_bytes"] == len(payload)
        try:
            session._on_data(b"!")
            try:
                os.write(viewer, b"late operator input")
            except OSError as exc:
                assert exc.errno == errno.EIO
        finally:
            os.close(viewer)
        info = session.mirror_info()
        assert info["state"] == "failed"
        assert info["failure_reason"] == "output_overflow"
        assert info["pending_bytes"] == 0
        assert info["undelivered_bytes"] == len(payload) + 1
        for fd in (master, slave):
            with pytest.raises(OSError):
                os.fstat(fd)
        buf.read(len(payload) + 1, timeout=0.01)
        os.write(port.rx_write, b"still receiving")
        assert _read_buffer(buf, len(b"still receiving")) == b"still receiving"
        assert session.alive
        assert port.writes == []
        warnings = [record for record in caplog.records if record.levelname == "WARNING"]
        assert len(warnings) == 1

    def test_unrecoverable_output_error_keeps_serial_reader_running(self, rw_session, monkeypatch):
        port = _PipePort()
        buf, _writer, session = rw_session(port)
        master = session._master_fd
        original_write = os.write

        def failed_write(fd, data):
            if fd == master:
                raise OSError(errno.EIO, "unavailable")
            return original_write(fd, data)

        monkeypatch.setattr("serial_mcp_server.port.mirror.os.write", failed_write)
        session._on_data(b"undelivered")
        assert session.mirror_info()["state"] == "failed"
        assert session.mirror_info()["failure_reason"] == "pty_write_failed"
        assert session.mirror_info()["undelivered_bytes"] == len(b"undelivered")
        assert _read_buffer(buf, len(b"undelivered")) == b"undelivered"
        os.write(port.rx_write, b"after failure")
        assert _read_buffer(buf, len(b"after failure")) == b"after failure"
        assert session.alive

    def test_close_counts_pending_bytes_and_is_idempotent(self, rw_session, monkeypatch):
        port = _PipePort()
        _buf, _writer, session = rw_session(port)
        master = session._master_fd
        original_write = os.write

        def stalled_write(fd, data):
            if fd == master:
                raise BlockingIOError()
            return original_write(fd, data)

        monkeypatch.setattr("serial_mcp_server.port.mirror.os.write", stalled_write)
        session._on_data(b"pending")
        session.stop()
        info = session.mirror_info()
        assert info["state"] == "closed"
        assert info["failure_reason"] is None
        assert info["pending_bytes"] == 0
        assert info["undelivered_bytes"] == len(b"pending")
        read_fd, write_fd = os.pipe()
        try:
            session.stop()
            assert session.mirror_info() == info
            os.write(write_fd, b"descriptor reuse")
            assert os.read(read_fd, 100) == b"descriptor reuse"
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_unrecoverable_input_error_keeps_serial_reader_running(self, rw_session, monkeypatch):
        port = _PipePort()
        buf, _writer, session = rw_session(port)
        master = session._master_fd
        original_read = os.read

        def failed_read(fd, count):
            if fd == master:
                raise OSError(errno.EIO, "unavailable")
            return original_read(fd, count)

        monkeypatch.setattr("serial_mcp_server.port.mirror.os.read", failed_read)
        os.write(session._slave_fd, b"input triggering failure")
        deadline = time.monotonic() + WAIT_S
        while session.mirror_info().get("state") != "failed" and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        assert session.mirror_info()["failure_reason"] == "pty_read_failed"
        os.write(port.rx_write, b"still alive")
        assert _read_buffer(buf, len(b"still alive")) == b"still alive"
        assert session.alive
        assert port.writes == []

    def test_temporary_blocking_retries_without_more_serial_input(self, rw_session, monkeypatch):
        port = _PipePort()
        buf, _writer, session = rw_session(port)
        original_write = os.write
        blocked = threading.Event()
        resume = threading.Event()

        def stalled_write(fd, data):
            if fd == session._master_fd and not resume.is_set():
                blocked.set()
                raise BlockingIOError()
            return original_write(fd, data)

        monkeypatch.setattr("serial_mcp_server.port.mirror.os.write", stalled_write)
        payload = b"first\r\nsecond\x1b[1Gthird\n"
        session._on_data(payload)
        assert blocked.wait(WAIT_S)
        assert buf.read(len(payload), timeout=0.05) == payload
        resume.set()

        assert _read_pty(session._slave_fd, len(payload)) == payload


def _read_buffer(buffer: SerialBuffer, count: int) -> bytes:
    received = bytearray()
    deadline = time.monotonic() + WAIT_S
    while len(received) < count and time.monotonic() < deadline:
        received.extend(buffer.read(count - len(received), timeout=0.05))
    return bytes(received)


@pytest.mark.skipif(not _IS_UNIX, reason="PTY mirror requires Unix")
def test_failed_raw_mode_closes_descriptors_and_releases_factory_index(tmp_path, monkeypatch):
    port = MagicMock(baudrate=115200)
    buffer = SerialBuffer()
    writer = SerialWriter(port)
    base = str(tmp_path / "mirror")
    allocated = []
    original_openpty = os.openpty

    def tracked_openpty():
        pair = original_openpty()
        allocated.extend(pair)
        return pair

    with monkeypatch.context() as fault:
        fault.setattr("serial_mcp_server.port.mirror.os.openpty", tracked_openpty)
        fault.setattr(
            "serial_mcp_server.port.mirror.tty.setraw", MagicMock(side_effect=OSError("raw failed"))
        )
        try:
            with pytest.raises(OSError, match="raw failed"):
                create_reader(port, buffer, "rw", base, writer=writer)
            for fd in allocated:
                with pytest.raises(OSError):
                    os.fstat(fd)
        finally:
            # Preserve resources even when the old constructor incorrectly succeeds.
            for fd in allocated:
                try:
                    os.close(fd)
                except OSError:
                    pass

    reader = create_reader(port, buffer, "rw", base, writer=writer)
    try:
        info = reader.mirror_info()
        assert info is not None
        assert info["link"] == base + "0"
    finally:
        reader.stop()
