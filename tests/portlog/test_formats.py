"""Tests for the text timestamps and the lines and raw formats in portlog/formats.py."""

from __future__ import annotations

import io
import json
import time
from datetime import UTC, datetime

import pytest

from serial_mcp_server import config
from serial_mcp_server.config import ConfigError, Settings
from serial_mcp_server.portlog.formats import (
    KIND_EVENT,
    KIND_OVERFLOW,
    KIND_RX,
    KIND_TX,
    LOG_ADMISSION_FAILED_EVENT,
    Entry,
    LinesWriter,
    RawWriter,
    TextTimestamps,
    TextWriter,
    WriterOptions,
)
from serial_mcp_server.portlog.log import PortLog
from serial_mcp_server.portlog.request import LogRequest, resolve_log_request
from tests.helpers import read_jsonl

WALL = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 8, 14, 5, 30)


class Sink(io.BytesIO):
    """A file whose contents stay readable after the writer closes it."""

    closed_by_writer = False

    def close(self) -> None:
        self.closed_by_writer = True


def rx(offset: float, data: bytes, wall: datetime = WALL) -> Entry:
    return Entry(KIND_RX, wall, offset, data=data)


def tx(offset: float, data: bytes) -> Entry:
    return Entry(KIND_TX, WALL, offset, data=data, source="mcp")


def event(offset: float, name: str, **fields) -> Entry:
    return Entry(KIND_EVENT, WALL, offset, name=name, fields=fields)


def drive(writer, entries) -> None:
    for entry in entries:
        writer.write(entry)
        writer.tick(entry.offset)


def lines_of(sink: Sink) -> list[dict]:
    return [json.loads(line) for line in sink.getvalue().decode().splitlines()]


def text_writer(mode: str) -> tuple[TextWriter, Sink]:
    sink = Sink()
    return TextWriter(sink, WriterOptions(text_timestamps=TextTimestamps(mode))), sink


class TestTextTimestamps:
    def test_event_loss_and_terminal_admission_failure_have_visible_markers(self):
        writer, sink = text_writer("off")
        loss = Entry(KIND_OVERFLOW, WALL, 1.0, fields={"dropped_bytes": 0, "dropped_events": 2})
        drive(writer, [rx(0.0, b"prefix"), loss, event(2.0, LOG_ADMISSION_FAILED_EVENT, reason="Stopped.")])
        assert sink.getvalue() == (
            b"prefix\n--- serial-mcp: 0 bytes not logged; 2 events not logged ---\n"
            b"--- serial-mcp: Stopped. ---\n"
        )

    def test_off_leaves_the_received_bytes_untouched(self):
        writer, sink = text_writer("off")
        drive(writer, [rx(1.0, b"a\nb"), rx(2.0, b"c\n"), tx(2.5, b"ls\n")])
        assert sink.getvalue() == b"a\nbc\n"

    def test_elapsed_prefixes_each_line_with_time_since_the_first_byte_and_delta(self):
        writer, sink = text_writer("elapsed")
        drive(writer, [rx(2.0, b"boot\n"), rx(2.5, b"ab"), rx(2.75, b"c\n"), rx(4.0, b"last\n")])

        assert sink.getvalue() == (
            b"[0.000000 0.000000] boot\n[0.500000 0.500000] abc\n[2.000000 1.500000] last\n"
        )

    def test_a_line_split_across_two_chunks_gets_one_prefix_from_its_first_byte(self):
        writer, sink = text_writer("elapsed")
        drive(writer, [rx(0.0, b"hel"), rx(0.9, b"lo\n")])
        assert sink.getvalue() == b"[0.000000 0.000000] hello\n"

    def test_several_lines_in_one_chunk_each_get_a_prefix_with_a_zero_delta(self):
        writer, sink = text_writer("elapsed")
        drive(writer, [rx(1.0, b"a\n\nb\n")])
        assert sink.getvalue() == (b"[0.000000 0.000000] a\n[0.000000 0.000000] \n[0.000000 0.000000] b\n")

    def test_system_uses_utc_wall_clock_time_and_the_delta(self):
        writer, sink = text_writer("system")
        drive(
            writer,
            [
                rx(0.0, b"x\n", datetime(2026, 10, 8, 12, 0, 1, 500000, tzinfo=UTC)),
                rx(0.25, b"y\n", datetime(2026, 10, 8, 12, 0, 1, 750000, tzinfo=UTC)),
            ],
        )
        assert sink.getvalue() == b"[12:00:01.500000 0.000000] x\n[12:00:01.750000 0.250000] y\n"

    def test_transmitted_bytes_and_events_are_never_written(self):
        writer, sink = text_writer("elapsed")
        drive(writer, [tx(0.0, b"ls\n"), event(0.1, "dtr", value=True), rx(0.2, b"ok\n")])
        assert sink.getvalue() == b"[0.000000 0.000000] ok\n"

    def test_the_overflow_marker_has_no_prefix_and_the_next_line_does(self):
        writer, sink = text_writer("elapsed")
        marker = Entry(KIND_OVERFLOW, WALL, 1.0, fields={"dropped_bytes": 7})
        drive(writer, [rx(0.0, b"par"), marker, rx(2.0, b"next\n")])

        assert sink.getvalue() == (
            b"[0.000000 0.000000] par\n--- serial-mcp: 7 bytes not logged ---\n[2.000000 2.000000] next\n"
        )

    def test_invalid_utf8_passes_through_with_a_prefix(self):
        writer, sink = text_writer("elapsed")
        drive(writer, [rx(0.0, b"\xff\xfe\n")])
        assert sink.getvalue() == b"[0.000000 0.000000] \xff\xfe\n"


class TestRawWriter:
    def test_holds_exactly_the_received_bytes(self):
        sink = Sink()
        writer = RawWriter(sink, WriterOptions())
        chunks = [b"\x00\xffabc", b"\r\n", b"\x80"]
        drive(
            writer,
            [
                rx(0.0, chunks[0]),
                tx(0.1, b"ignored"),
                event(0.2, "dtr"),
                rx(0.3, chunks[1]),
                rx(0.4, chunks[2]),
            ],
        )
        assert sink.getvalue() == b"".join(chunks)


def lines_writer(idle_s: float = 0.5) -> tuple[LinesWriter, Sink]:
    sink = Sink()
    return LinesWriter(sink, WriterOptions(), idle_s), sink


class TestLinesWriter:
    def test_records_each_direction_with_the_line_start_time_and_strips_the_terminator(self):
        writer, sink = lines_writer()
        drive(writer, [rx(1.0, b"U-Boot 2024.01\r\n"), tx(1.5, b"version\n"), rx(2.0, b"a\r\r\n")])

        assert [(e["dir"], e["text"], e["t"]) for e in lines_of(sink)] == [
            ("rx", "U-Boot 2024.01", 1.0),
            ("tx", "version", 1.5),
            ("rx", "a\r", 2.0),
        ]
        assert lines_of(sink)[0]["ts"] == "2026-10-08T12:00:00.000000Z"

    def test_a_chunk_with_several_lines_and_a_blank_line_yields_one_entry_each(self):
        writer, sink = lines_writer()
        drive(writer, [rx(0.0, b"a\n\nb\npar")])
        assert [e["text"] for e in lines_of(sink)] == ["a", "", "b"]

    def test_a_line_split_across_chunks_carries_the_first_chunks_time(self):
        writer, sink = lines_writer()
        drive(writer, [rx(1.0, b"hel"), rx(1.2, b"lo\n")])
        (entry,) = lines_of(sink)
        assert (entry["text"], entry["t"]) == ("hello", 1.0)
        assert "partial" not in entry and "continued" not in entry

    def test_an_idle_prompt_is_written_as_partial_only_after_the_idle_time(self):
        writer, sink = lines_writer(idle_s=0.5)
        writer.write(rx(1.0, b"=> "))

        writer.tick(1.4)
        assert sink.getvalue() == b""

        writer.tick(1.5)
        assert lines_of(sink) == [
            {"ts": "2026-10-08T12:00:00.000000Z", "t": 1.0, "dir": "rx", "text": "=> ", "partial": True}
        ]

    def test_silence_is_judged_by_entry_times_even_when_no_tick_ran_between_them(self):
        # A backlogged disk thread writes both entries back to back.
        writer, sink = lines_writer(idle_s=0.5)
        writer.write(rx(1.0, b"=> "))
        writer.write(rx(3.0, b"ls\n"))

        assert [(e["text"], e.get("partial"), e.get("continued")) for e in lines_of(sink)] == [
            ("=> ", True, None),
            ("ls", None, True),
        ]

    def test_a_quick_follow_up_still_merges_into_one_line_without_a_tick(self):
        writer, sink = lines_writer(idle_s=0.5)
        writer.write(rx(1.0, b"=> "))
        writer.write(rx(1.2, b"ls\n"))
        assert [(e["text"], e.get("partial")) for e in lines_of(sink)] == [("=> ls", None)]

    def test_the_file_is_closed_even_when_the_final_flush_fails(self):
        writer, sink = lines_writer()
        writer.write(rx(1.0, b"unfinished"))
        sink.write = lambda data: (_ for _ in ()).throw(OSError("disk full"))

        with pytest.raises(OSError, match="disk full"):
            writer.close()

        assert sink.closed_by_writer

    def test_bytes_typed_after_a_partial_prompt_are_marked_continued_and_the_next_line_is_not(self):
        writer, sink = lines_writer()
        writer.write(rx(1.0, b"=> "))
        writer.tick(2.0)
        writer.write(rx(5.0, b"ls\nnext\n"))

        flags = [(e["text"], e.get("partial"), e.get("continued")) for e in lines_of(sink)]
        assert flags == [("=> ", True, None), ("ls", None, True), ("next", None, None)]

    def test_joining_a_partial_with_its_continuations_restores_the_line(self):
        writer, sink = lines_writer()
        writer.write(rx(1.0, b"login: "))
        writer.tick(2.0)
        writer.write(rx(9.0, b"root\n"))
        assert "".join(e["text"] for e in lines_of(sink)) == "login: root"

    def test_a_newline_alone_after_a_partial_closes_the_line_with_an_empty_continuation(self):
        writer, sink = lines_writer()
        writer.write(rx(1.0, b"=> "))
        writer.tick(2.0)
        writer.write(rx(3.0, b"\n"))
        assert [(e["text"], e.get("continued")) for e in lines_of(sink)] == [("=> ", None), ("", True)]

    def test_each_direction_keeps_its_own_pending_line(self):
        writer, sink = lines_writer()
        writer.write(rx(1.0, b"rx part"))
        writer.write(tx(1.1, b"tx part"))
        writer.write(rx(1.2, b" more\n"))
        assert [(e["dir"], e["text"]) for e in lines_of(sink)] == [("rx", "rx part more")]
        assert writer.pending_deadline() == pytest.approx(1.1 + 0.5)

    def test_pending_deadline_is_none_when_no_line_waits(self):
        writer, _ = lines_writer()
        assert writer.pending_deadline() is None
        writer.write(rx(1.0, b"done\n"))
        assert writer.pending_deadline() is None

    def test_close_writes_the_remaining_partial_line_and_closes_the_file(self):
        writer, sink = lines_writer()
        writer.write(rx(1.0, b"unfinished"))
        writer.close()
        assert lines_of(sink) == [
            {
                "ts": "2026-10-08T12:00:00.000000Z",
                "t": 1.0,
                "dir": "rx",
                "text": "unfinished",
                "partial": True,
            }
        ]
        assert sink.closed_by_writer

    def test_a_mark_flushes_pending_partials_first_and_keeps_its_place(self):
        writer, sink = lines_writer()
        writer.write(rx(1.0, b"par"))
        writer.write(event(1.1, "mark", label="step 2"))
        writer.write(rx(1.2, b"tial\n"))

        assert [(e.get("text"), e.get("event")) for e in lines_of(sink)] == [
            ("par", None),
            (None, "mark"),
            ("tial", None),
        ]
        assert lines_of(sink)[1]["label"] == "step 2"

    def test_admission_failure_flushes_prefix_and_records_terminal_reason(self):
        writer, sink = lines_writer()
        writer.write(rx(1.0, b"prefix"))
        writer.write(event(1.1, LOG_ADMISSION_FAILED_EVENT, reason="Stopped."))
        records = lines_of(sink)
        assert [(r.get("text"), r.get("event")) for r in records] == [
            ("prefix", None),
            (None, "log_admission_failed"),
        ]
        assert records[-1]["reason"] == "Stopped."

    def test_overflow_is_recorded_and_other_control_events_are_not(self):
        writer, sink = lines_writer()
        writer.write(event(0.0, "dtr", value=False))
        writer.write(Entry(KIND_OVERFLOW, WALL, 0.1, fields={"dropped_bytes": 12}))

        (entry,) = lines_of(sink)
        assert (entry["event"], entry["dropped_bytes"]) == ("log_overflow", 12)

    def test_decoding_follows_a_configure_event(self):
        writer, sink = lines_writer()
        writer.write(rx(0.0, "é\n".encode("latin-1")))
        writer.write(event(0.1, "configure", encoding="latin-1"))
        writer.write(rx(0.2, "é\n".encode("latin-1")))
        assert [e["text"] for e in lines_of(sink)] == ["�", "é"]


class TestThroughPortLog:
    def test_choosing_text_and_lines_writes_both_files_and_no_jsonl_file(self, tmp_path):
        log = PortLog.start(LogRequest(("text", "lines"), tmp_path), "utf-8", NOW)
        log.rx(b"hello\r\n")
        log.close()

        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "serial-mcp-log-08102026-1405.lines.jsonl",
            "serial-mcp-log-08102026-1405.txt",
        ]
        assert log.files["text"].read_bytes() == b"hello\r\n"
        assert read_jsonl(log.files["lines"])[0]["text"] == "hello"

    def test_raw_file_is_byte_identical_to_the_received_input(self, tmp_path):
        log = PortLog.start(LogRequest(("raw", "text"), tmp_path), "utf-8", NOW)
        chunks = [b"\xff\x00abc", b"\r\n\x80"]
        for chunk in chunks:
            log.rx(chunk)
        log.tx(b"ignored", "mcp")
        log.close()

        assert log.files["raw"].read_bytes() == b"".join(chunks)
        assert log.files["raw"].suffix == ".bin"
        assert log.files["text"].read_bytes() == b"".join(chunks)

    def test_the_disk_thread_flushes_an_idle_prompt_without_further_traffic(self, tmp_path):
        sink = open(tmp_path / "p.lines.jsonl", "wb")  # noqa: SIM115 - closed by the log
        log = PortLog(
            {"lines": LinesWriter(sink, WriterOptions(), idle_s=0.05)}, {"lines": tmp_path / "p.lines.jsonl"}
        )
        log.rx(b"=> ")

        deadline = time.monotonic() + 2.0
        content = b""
        while not content and time.monotonic() < deadline:
            time.sleep(0.01)
            content = (tmp_path / "p.lines.jsonl").read_bytes()
        log.close()

        assert json.loads(content)["partial"] is True

    def test_text_timestamps_apply_through_the_log(self, tmp_path):
        request = LogRequest(("text",), tmp_path, text_timestamps=TextTimestamps.ELAPSED)
        log = PortLog.start(request, "utf-8", NOW)
        log.rx(b"hello\n")
        log.close()

        assert log.files["text"].read_text().startswith("[0.000000 0.000000] hello\n")


class TestTimestampSetting:
    @staticmethod
    def configured(tmp_path, **environ: str) -> Settings:
        return config.load({"SERIAL_MCP_SPEC_ROOT": str(tmp_path), **environ})

    def test_defaults_to_off(self, tmp_path):
        assert resolve_log_request(self.configured(tmp_path), True).text_timestamps == "off"

    def test_environment_sets_the_default(self, tmp_path):
        settings = self.configured(tmp_path, SERIAL_MCP_LOG_TEXT_TIMESTAMPS="system")
        assert resolve_log_request(settings, True).text_timestamps == "system"

    def test_argument_overrides_the_environment(self, tmp_path):
        settings = self.configured(tmp_path, SERIAL_MCP_LOG_TEXT_TIMESTAMPS="system")
        assert resolve_log_request(settings, True, None, "elapsed").text_timestamps == "elapsed"
        assert resolve_log_request(settings, True, None, "off").text_timestamps == "off"

    @pytest.mark.parametrize("bad", ["fast", "", 3, True])
    def test_invalid_argument_names_the_argument(self, tmp_path, bad):
        with pytest.raises(ValueError, match="log_text_timestamps"):
            resolve_log_request(self.configured(tmp_path), True, None, bad)

    def test_invalid_environment_value_names_the_variable(self, tmp_path):
        with pytest.raises(ConfigError, match="SERIAL_MCP_LOG_TEXT_TIMESTAMPS"):
            self.configured(tmp_path, SERIAL_MCP_LOG_TEXT_TIMESTAMPS="fast")
