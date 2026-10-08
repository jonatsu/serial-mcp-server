"""Tests for serial_mcp_server.writer — pacing, ordering, cancellation, and shutdown."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from serial_mcp_server.writer import (
    NO_PACING,
    Pacing,
    SerialWriter,
    WriteAborted,
    WriterClosed,
    write_serial_payload,
)

WAIT_S = 2.0


class RecordingEvent(threading.Event):
    """Event whose ``wait`` records the delay and returns at once."""

    def __init__(self, log: list) -> None:
        super().__init__()
        self._log = log

    def wait(self, timeout: float | None = None) -> bool:
        self._log.append(("sleep", timeout))
        return self.is_set()


def _recording_port(events: list, accepted: int | None = None) -> MagicMock:
    ser = MagicMock()

    def record_write(data):
        events.append(("write", data))
        return len(data) if accepted is None else accepted

    ser.write.side_effect = record_write
    ser.flush.side_effect = lambda: events.append(("flush",))
    return ser


class BlockingPort:
    """Port whose first ``write`` holds until released, recording every write."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self._first:
            self._first = False
            self.entered.set()
            assert self.release.wait(WAIT_S)
        return len(data)

    def flush(self) -> None:
        pass


@pytest.fixture()
def started_writer():
    writers: list[SerialWriter] = []

    def make(ser, pacing: Pacing | None = None) -> SerialWriter:
        writer = SerialWriter(ser, pacing or NO_PACING)
        writer.start()
        writers.append(writer)
        return writer

    yield make
    for writer in writers:
        writer.stop()
        writer.join()


class TestWriteSerialPayload:
    def test_unpaced_write_is_a_single_call(self):
        ser = MagicMock()
        ser.write.return_value = 5
        assert write_serial_payload(ser, b"ABCDE", NO_PACING) == 5
        ser.write.assert_called_once_with(b"ABCDE")

    def test_character_delay_starts_after_output_buffer_drains_without_trailing_wait(self):
        events: list = []
        n_written = write_serial_payload(
            _recording_port(events), b"ABC", Pacing(inter_char_delay_ms=5), RecordingEvent(events)
        )

        assert n_written == 3
        assert events == [
            ("write", b"A"),
            ("flush",),
            ("sleep", 0.005),
            ("write", b"B"),
            ("flush",),
            ("sleep", 0.005),
            ("write", b"C"),
            ("flush",),
        ]

    def test_character_pacing_stops_after_a_short_write(self):
        events: list = []
        n_written = write_serial_payload(
            _recording_port(events, accepted=0), b"ABC", Pacing(inter_char_delay_ms=5), RecordingEvent(events)
        )

        assert n_written == 0
        assert events == [("write", b"A"), ("flush",)]

    def test_chunk_delay_starts_after_output_buffer_drains_without_trailing_wait(self):
        events: list = []
        n_written = write_serial_payload(
            _recording_port(events),
            b"ABCDE",
            Pacing(chunk_size=2, chunk_delay_ms=5),
            RecordingEvent(events),
        )

        assert n_written == 5
        assert events == [
            ("write", b"AB"),
            ("flush",),
            ("sleep", 0.005),
            ("write", b"CD"),
            ("flush",),
            ("sleep", 0.005),
            ("write", b"E"),
            ("flush",),
        ]

    def test_chunk_pacing_stops_after_a_short_write(self):
        events: list = []
        n_written = write_serial_payload(
            _recording_port(events, accepted=1),
            b"ABCDE",
            Pacing(chunk_size=2, chunk_delay_ms=5),
            RecordingEvent(events),
        )

        assert n_written == 1
        assert events == [("write", b"AB"), ("flush",)]

    def test_set_cancel_event_aborts_with_bytes_written_so_far(self):
        cancel = threading.Event()
        ser = MagicMock()

        def write_then_cancel(data):
            cancel.set()
            return len(data)

        ser.write.side_effect = write_then_cancel
        with pytest.raises(WriteAborted) as excinfo:
            write_serial_payload(ser, b"ABC", Pacing(chunk_size=1, chunk_delay_ms=5), cancel)

        assert excinfo.value.bytes_written == 1
        ser.write.assert_called_once_with(b"A")


class TestSerialWriter:
    def test_job_resolves_to_bytes_written_and_flushes_unpaced_writes(self, started_writer):
        ser = MagicMock()
        ser.write.return_value = 3
        writer = started_writer(ser)

        assert writer.submit(b"abc").future.result(WAIT_S) == 3
        ser.flush.assert_called_once_with()

    def test_jobs_run_in_submission_order_without_interleaving(self, started_writer):
        ser = BlockingPort()
        writer = started_writer(ser, Pacing(chunk_size=2, chunk_delay_ms=1))

        first = writer.submit(b"AAAA")
        assert ser.entered.wait(WAIT_S)
        second = writer.submit(b"BBBB")
        ser.release.set()
        first.future.result(WAIT_S)
        second.future.result(WAIT_S)

        assert ser.writes == [b"AA", b"AA", b"BB", b"BB"]

    def test_job_uses_writer_pacing_unless_overridden(self, started_writer):
        ser = MagicMock()
        ser.write.side_effect = len
        writer = started_writer(ser, Pacing(chunk_size=2, chunk_delay_ms=1))

        writer.submit(b"ABCD").future.result(WAIT_S)
        writer.submit(b"ABCD", NO_PACING).future.result(WAIT_S)

        assert [call.args[0] for call in ser.write.call_args_list] == [b"AB", b"CD", b"ABCD"]

    def test_pending_bytes_are_counted_per_source_until_the_job_finishes(self, started_writer):
        ser = BlockingPort()
        writer = started_writer(ser)

        first = writer.submit(b"abc", source="mcp")
        assert ser.entered.wait(WAIT_S)
        second = writer.submit(b"de", source="mirror")
        assert (writer.pending_bytes("mcp"), writer.pending_bytes("mirror")) == (3, 2)

        ser.release.set()
        first.future.result(WAIT_S)
        second.future.result(WAIT_S)
        # The count drops just after the future resolves, so wait on the thread.
        writer.stop()
        writer.join()
        assert (writer.pending_bytes("mcp"), writer.pending_bytes("mirror")) == (0, 0)

    def test_job_cancelled_while_queued_writes_nothing_and_writer_keeps_running(self, started_writer):
        ser = BlockingPort()
        writer = started_writer(ser)

        first = writer.submit(b"first")
        assert ser.entered.wait(WAIT_S)
        cancelled = writer.submit(b"never")
        assert cancelled.future.cancel()
        ser.release.set()
        first.future.result(WAIT_S)

        assert writer.submit(b"last").future.result(WAIT_S) == 4
        assert ser.writes == [b"first", b"last"]
        assert writer.alive

    def test_write_error_reaches_the_caller_and_writer_keeps_running(self, started_writer):
        ser = MagicMock()
        ser.write.side_effect = [OSError("boom"), 2]
        writer = started_writer(ser)

        with pytest.raises(OSError, match="boom"):
            writer.submit(b"ab").future.result(WAIT_S)
        assert writer.submit(b"cd").future.result(WAIT_S) == 2

    def test_stop_fails_queued_jobs_and_ends_the_thread(self, started_writer):
        ser = BlockingPort()
        writer = started_writer(ser)

        first = writer.submit(b"first")
        assert ser.entered.wait(WAIT_S)
        queued = writer.submit(b"queued")
        writer.stop()
        ser.release.set()
        writer.join()

        assert first.future.result(WAIT_S) == 5
        with pytest.raises(WriterClosed):
            queued.future.result(WAIT_S)
        assert ser.writes == [b"first"]
        assert not writer.alive

    def test_submit_after_stop_raises(self, started_writer):
        writer = started_writer(MagicMock())
        writer.stop()
        with pytest.raises(WriterClosed):
            writer.submit(b"x")

    def test_stop_cancels_a_running_paced_write_between_segments(self, started_writer):
        ser = BlockingPort()
        writer = started_writer(ser, Pacing(chunk_size=1, chunk_delay_ms=1000))

        job = writer.submit(b"ABCDEF")
        assert ser.entered.wait(WAIT_S)
        writer.stop()
        ser.release.set()
        writer.join()

        with pytest.raises(WriteAborted) as excinfo:
            job.future.result(WAIT_S)
        assert excinfo.value.bytes_written == 1
        assert ser.writes == [b"A"]

    def test_discard_pending_fails_queued_jobs_and_keeps_the_writer_usable(self, started_writer):
        ser = BlockingPort()
        writer = started_writer(ser)

        first = writer.submit(b"first")
        assert ser.entered.wait(WAIT_S)
        queued = writer.submit(b"queued")
        writer.discard_pending()
        ser.release.set()
        first.future.result(WAIT_S)

        with pytest.raises(WriteAborted) as excinfo:
            queued.future.result(WAIT_S)
        assert excinfo.value.bytes_written == 0
        assert writer.submit(b"after").future.result(WAIT_S) == 5

    def test_cancelling_a_finished_job_is_a_no_op(self, started_writer):
        writer = started_writer(MagicMock())
        job = writer.submit(b"x")
        job.future.exception(WAIT_S)
        assert not job.future.cancel()
