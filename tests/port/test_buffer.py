"""Tests for serial_mcp_server.port.buffer — SerialBuffer."""

from __future__ import annotations

import sys
import threading
import time

from serial_mcp_server.port.buffer import SerialBuffer

# MirrorSession is Unix-only
_IS_UNIX = sys.platform != "win32"
if _IS_UNIX:
    pass


# ---------------------------------------------------------------------------
# SerialBuffer
# ---------------------------------------------------------------------------


class TestSerialBuffer:
    def test_write_and_read(self):
        buf = SerialBuffer()
        buf.write(b"hello")
        result = buf.read(10, timeout=1.0)
        assert result == b"hello"

    def test_read_returns_up_to_nbytes(self):
        buf = SerialBuffer()
        buf.write(b"hello world")
        result = buf.read(5, timeout=1.0)
        assert result == b"hello"
        # Remainder stays in buffer
        result2 = buf.read(100, timeout=0.01)
        assert result2 == b" world"

    def test_read_timeout_empty(self):
        buf = SerialBuffer()
        start = time.monotonic()
        result = buf.read(10, timeout=0.05)
        elapsed = time.monotonic() - start
        assert result == b""
        assert elapsed >= 0.04

    def test_read_blocks_until_data(self):
        buf = SerialBuffer()

        def delayed_write():
            time.sleep(0.05)
            buf.write(b"data")

        t = threading.Thread(target=delayed_write)
        t.start()
        result = buf.read(10, timeout=1.0)
        t.join()
        assert result == b"data"

    def test_read_until_delimiter(self):
        buf = SerialBuffer()
        buf.write(b"line1\r\nline2\r\n")
        result = buf.read_until(b"\r\n", max_bytes=100, timeout=1.0)
        assert result == b"line1\r\n"

    def test_read_until_max_bytes(self):
        buf = SerialBuffer()
        buf.write(b"a very long line without newline")
        result = buf.read_until(b"\n", max_bytes=10, timeout=0.05)
        assert result == b"a very lon"
        assert len(result) == 10

    def test_read_until_timeout(self):
        buf = SerialBuffer()
        buf.write(b"no newline here")
        result = buf.read_until(b"\n", max_bytes=1000, timeout=0.05)
        assert result == b"no newline here"

    def test_read_until_blocks_until_delimiter(self):
        buf = SerialBuffer()

        def delayed_write():
            time.sleep(0.05)
            buf.write(b"hello\n")

        t = threading.Thread(target=delayed_write)
        t.start()
        result = buf.read_until(b"\n", max_bytes=100, timeout=1.0)
        t.join()
        assert result == b"hello\n"

    def test_clear(self):
        buf = SerialBuffer()
        buf.write(b"data")
        assert buf.available == 4
        buf.clear()
        assert buf.available == 0

    def test_available(self):
        buf = SerialBuffer()
        assert buf.available == 0
        buf.write(b"abc")
        assert buf.available == 3

    def test_max_size_trims_oldest(self):
        buf = SerialBuffer(max_size=10)
        buf.write(b"0123456789")
        assert buf.available == 10
        buf.write(b"AB")
        assert buf.available == 10
        result = buf.read(10, timeout=0.01)
        assert result == b"23456789AB"

    def test_write_empty_noop(self):
        buf = SerialBuffer()
        buf.write(b"")
        assert buf.available == 0

    def test_concurrent_write_read(self):
        """Multiple writers + one reader, no crashes."""
        buf = SerialBuffer()
        total_written = 0
        lock = threading.Lock()

        def writer(chunk: bytes, count: int):
            nonlocal total_written
            for _ in range(count):
                buf.write(chunk)
                with lock:
                    total_written += len(chunk)
                time.sleep(0.001)

        threads = [
            threading.Thread(target=writer, args=(b"A" * 10, 50)),
            threading.Thread(target=writer, args=(b"B" * 10, 50)),
        ]
        for t in threads:
            t.start()

        read_total = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            data = buf.read(100, timeout=0.01)
            read_total += len(data)
            if read_total >= 1000:
                break

        for t in threads:
            t.join()

        # Drain remaining
        remaining = buf.read(10000, timeout=0.01)
        read_total += len(remaining)
        assert read_total == total_written
