"""``SerialConnection``: one open port with its writer, reader, buffer, and log slot."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import serial as pyserial

from serial_mcp_server.port.buffer import SerialBuffer
from serial_mcp_server.port.reader import ReaderThread
from serial_mcp_server.port.writer import SerialWriter
from serial_mcp_server.portlog.log import PortLogSlot

logger = logging.getLogger("serial_mcp_server")


@dataclass
class SerialConnection:
    """A managed serial port connection."""

    connection_id: str
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    timeout: float  # seconds
    write_timeout: float  # seconds
    encoding: str
    newline: str
    ser: pyserial.Serial
    writer: SerialWriter
    buffer: SerialBuffer = field(default_factory=SerialBuffer)
    reader: ReaderThread | None = None
    opened_at: float = field(default_factory=time.time)
    last_seen_ts: float = field(default_factory=time.time)
    spec: dict[str, Any] | None = None

    @property
    def log_slot(self) -> PortLogSlot:
        """The port log holder shared with this connection's writer and reader."""
        return self.writer.log_slot


def stop_connections(conns: list[SerialConnection]) -> None:
    """Signal every connection's threads before joining any, then close the logs and ports."""
    for conn in conns:
        conn.writer.stop()
        if conn.reader is not None:
            conn.reader.signal_stop()
    for conn in conns:
        conn.writer.join()
        if conn.reader is not None:
            try:
                conn.reader.join()
            except Exception:
                logger.warning("Reader for %s did not stop cleanly.", conn.port, exc_info=True)
        try:
            conn.log_slot.close_log()
        except Exception:
            logger.warning("Closing the port log for %s failed.", conn.port, exc_info=True)
        try:
            if conn.ser.is_open:
                conn.ser.close()
        except Exception:
            logger.warning("Closing %s failed.", conn.port, exc_info=True)
