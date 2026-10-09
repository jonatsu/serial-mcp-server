"""Tests for serial_mcp_server.port.reader — ReaderThread."""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock, PropertyMock

from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.reader import ReaderThread

# MirrorSession is Unix-only
_IS_UNIX = sys.platform != "win32"
if _IS_UNIX:
    pass


class TestReaderThread:
    def test_data_flows_to_buffer(self):
        buf = SerialBuffer()
        ser = MagicMock()
        type(ser).in_waiting = PropertyMock(side_effect=[5, 0, 0, 0])
        ser.read.side_effect = [b"hello", b"", b"", b""]

        reader = ReaderThread(ser, buf)
        reader.start()
        time.sleep(0.1)
        reader.stop()

        assert buf.available >= 5
        assert buf.read(5, timeout=0.01) == b"hello"

    def test_start_stop(self):
        buf = SerialBuffer()
        ser = MagicMock()
        type(ser).in_waiting = PropertyMock(return_value=0)
        ser.read.return_value = b""

        reader = ReaderThread(ser, buf)
        reader.start()
        assert reader.alive
        reader.stop()
        assert not reader.alive

    def test_mirror_info_returns_none(self):
        buf = SerialBuffer()
        ser = MagicMock()
        reader = ReaderThread(ser, buf)
        assert reader.mirror_info() is None

    def test_delivers_to_subscriber_after_log_and_buffer(self):
        buffer = SerialBuffer()
        events = []

        class LogSlot:
            def rx(self, data):
                events.append(("log", data))

        class Subscriber:
            def feed(self, data):
                events.append(("subscriber", data, buffer.available))

            def close(self, error=None):
                pass

        reader = ReaderThread(MagicMock(), buffer, LogSlot())
        reader.subscribe(Subscriber())

        reader._deliver(b"hello")

        assert events == [("log", b"hello"), ("subscriber", b"hello", 5)]
        assert buffer.read(5, timeout=0.01) == b"hello"


# ---------------------------------------------------------------------------
# MirrorSession (Unix only)
# ---------------------------------------------------------------------------
