"""Build the writer, reader, and port log around an open serial port.

``serial.open`` and the ``serial_mcp_script`` command share this opener. It never imports ``mcp``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import serial as pyserial

from serial_mcp_server.mirror import ReaderThread, SerialBuffer, create_reader
from serial_mcp_server.portlog import LogRequest, PortLog, PortLogSlot
from serial_mcp_server.portlog_formats import BUFFER_DROP_EVENT, OPEN_EVENT
from serial_mcp_server.state import SerialConnection
from serial_mcp_server.writer import Pacing, SerialWriter

logger = logging.getLogger("serial_mcp_server")


class PortLogStartError(OSError):
    """The requested port log could not be started; ``directory`` is where it was to go."""

    def __init__(self, directory: Path, cause: OSError) -> None:
        super().__init__(f"Could not start the port log in {directory}: {cause}")
        self.directory = directory


def make_read_buffer(slot: PortLogSlot) -> SerialBuffer:
    """A read buffer that records each trim of unread bytes as a ``buffer_drop`` port-log event."""
    return SerialBuffer(on_drop=lambda count: slot.event(BUFFER_DROP_EVENT, dropped_bytes=count))


def conn_config(conn: SerialConnection) -> dict[str, Any]:
    """Return a dict describing the connection's serial configuration."""
    pacing = conn.writer.pacing
    return {
        "port": conn.port,
        "baudrate": conn.baudrate,
        "bytesize": conn.bytesize,
        "parity": conn.parity,
        "stopbits": conn.stopbits,
        "timeout_ms": round(conn.timeout * 1000),
        "write_timeout_ms": round(conn.write_timeout * 1000),
        "inter_char_delay_ms": pacing.inter_char_delay_ms,
        "chunk_size": pacing.chunk_size,
        "chunk_delay_ms": pacing.chunk_delay_ms,
        "encoding": conn.encoding,
        "newline": repr(conn.newline),
    }


def _release(slot: PortLogSlot, writer: SerialWriter | None, reader: ReaderThread | None) -> None:
    """Stop and join whichever threads exist, then close the log; leaves the serial port to the caller.

    Never raises, because the caller is already propagating the error that made this necessary. A thread
    whose ``start`` failed cannot be joined, so a failing join is logged and the remaining steps still run.
    """
    if writer is not None:
        writer.stop()
    if reader is not None:
        reader.signal_stop()
    for owner in (writer, reader):
        if owner is not None:
            try:
                owner.join()
            except Exception:
                logger.warning("The %s did not stop cleanly.", type(owner).__name__, exc_info=True)
    try:
        slot.close_log()
    except Exception:
        logger.warning("Closing the port log failed.", exc_info=True)


def open_port_stack(
    ser: pyserial.Serial,
    *,
    port: str,
    baudrate: int,
    bytesize: int,
    parity: str,
    stopbits: float,
    timeout: float,
    write_timeout: float,
    encoding: str,
    newline: str,
    pacing: Pacing,
    log_request: LogRequest | None,
    connection_id: str,
    mirror_mode: str,
    mirror_link: str | None,
) -> SerialConnection:
    """Build and start the log, read buffer, writer, and reader for an already-open port.

    Blocking: starts files and threads, so call it off an event loop. The port log's first event
    is ``open``, recorded before the threads start so it precedes any bytes they deliver.

    Raises:
        PortLogStartError: *log_request* was given and the log could not start.

    Any failure releases the log and threads this function created and re-raises; the caller
    still owns *ser* and closes it.
    """
    slot = PortLogSlot()
    writer: SerialWriter | None = None
    reader: ReaderThread | None = None
    try:
        if log_request is not None:
            # Fail the open rather than run a session the caller asked to record without its log.
            try:
                slot.set(PortLog.start(log_request, encoding))
            except OSError as exc:
                raise PortLogStartError(log_request.directory, exc) from exc

        buffer = make_read_buffer(slot)
        writer = SerialWriter(ser, pacing, slot)
        reader = create_reader(ser, buffer, mirror_mode, mirror_link, writer=writer, log_slot=slot)
        conn = SerialConnection(
            connection_id=connection_id,
            port=port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout,
            write_timeout=write_timeout,
            encoding=encoding,
            newline=newline,
            ser=ser,
            writer=writer,
            buffer=buffer,
            reader=reader,
        )
        slot.event(OPEN_EVENT, config=conn_config(conn))
        writer.start()
        reader.start()
    except BaseException:
        _release(slot, writer, reader)
        raise
    return conn
