"""In-memory state for serial connections."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import serial as pyserial

from serial_mcp_server.mirror import ReaderThread, SerialBuffer
from serial_mcp_server.portlog import PortLogSlot
from serial_mcp_server.writer import SerialWriter

logger = logging.getLogger("serial_mcp_server")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class SerialState:
    """Central mutable state shared by all tool handlers.

    All mutations happen on the asyncio event loop (single-threaded), so no
    lock is needed.  Background reader threads only touch ``conn.buffer``
    and ``conn.ser``, which have their own synchronisation.
    """

    def __init__(self, max_connections: int = 10) -> None:
        self.connections: dict[str, SerialConnection] = {}
        self.max_connections = max_connections

    def generate_id(self) -> str:
        return f"s{uuid.uuid4().hex[:8]}"

    def get_connection(self, connection_id: str) -> SerialConnection:
        if connection_id not in self.connections:
            raise KeyError(
                f"Unknown connection_id: {connection_id}. "
                "Call serial.connections.list to see active connections."
            )
        return self.connections[connection_id]

    def add_connection(self, conn: SerialConnection) -> None:
        if len(self.connections) >= self.max_connections:
            raise RuntimeError(
                f"Maximum connections ({self.max_connections}) reached. "
                "Close a connection first. Set SERIAL_MCP_MAX_CONNECTIONS to adjust."
            )
        self.connections[conn.connection_id] = conn

    def remove_connection(self, connection_id: str) -> SerialConnection:
        if connection_id not in self.connections:
            raise KeyError(
                f"Unknown connection_id: {connection_id}. "
                "Call serial.connections.list to see active connections."
            )
        return self.connections.pop(connection_id)

    async def shutdown(self) -> None:
        """Stop all readers and writers and close all open serial ports."""
        conns = list(self.connections.values())
        self.connections.clear()
        await asyncio.to_thread(stop_connections, conns)


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
