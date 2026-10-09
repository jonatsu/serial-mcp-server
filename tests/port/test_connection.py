"""Tests for serial_mcp_server.port.connection — SerialConnection."""

from __future__ import annotations

from unittest.mock import MagicMock

from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.writer import SerialWriter


class TestSerialConnection:
    def test_defaults(self):
        conn = SerialConnection(
            connection_id="s1",
            port="/dev/ttyUSB0",
            baudrate=115200,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.2,
            write_timeout=0.2,
            encoding="utf-8",
            newline="\n",
            ser=MagicMock(),
            writer=SerialWriter(MagicMock()),
        )
        assert conn.opened_at > 0
        assert conn.last_seen_ts > 0
