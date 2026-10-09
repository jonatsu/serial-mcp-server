"""Shared fixtures for Serial MCP handler tests."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from serial_mcp_server import config
from serial_mcp_server.config import Settings
from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.connection import SerialConnection
from serial_mcp_server.port.writer import SerialWriter
from serial_mcp_server.state import SerialState


@pytest.fixture()
def settings(tmp_path):
    """Default settings with the data root, and so every log, file, and script directory, under tmp_path."""
    return config.load({"SERIAL_MCP_SPEC_ROOT": str(tmp_path / ".serial_mcp")})


@pytest.fixture()
def serial_state(settings: Settings):
    """Fresh SerialState instance using the test's settings."""
    return SerialState(settings)


@pytest.fixture()
def mock_serial():
    """MagicMock pyserial.Serial with sensible defaults."""
    ser = MagicMock()
    ser.is_open = True
    ser.in_waiting = 0
    ser.read.return_value = b""
    ser.write.return_value = 0
    ser.read_until.return_value = b""
    type(ser).dtr = PropertyMock(return_value=True)
    type(ser).rts = PropertyMock(return_value=True)
    return ser


@pytest.fixture()
def mock_reader():
    """MagicMock ReaderThread."""
    reader = MagicMock()
    reader.mirror_info.return_value = None
    return reader


@pytest.fixture()
def connected_entry(serial_state, mock_serial, mock_reader):
    """SerialConnection registered in serial_state as 's1'. Returns (state, conn)."""
    buf = SerialBuffer()
    writer = SerialWriter(mock_serial)
    writer.start()
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
        ser=mock_serial,
        writer=writer,
        buffer=buf,
        reader=mock_reader,
    )
    serial_state.connections["s1"] = conn
    yield serial_state, conn
    writer.stop()
    writer.join()
