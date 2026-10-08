"""Tests for serial_mcp_server.portlog — settings, file naming, formats, and overflow."""

from __future__ import annotations

import os
import stat
import threading
from datetime import UTC, datetime
from itertools import count

import pytest

from serial_mcp_server import portlog, portlog_formats
from serial_mcp_server.portlog import (
    LogRequest,
    PortLog,
    PortLogSlot,
    open_log_files,
    resolve_log_request,
)
from serial_mcp_server.portlog_formats import FORMATS, JsonlWriter, TextWriter, WriterOptions
from tests.helpers import read_jsonl

WAIT_S = 2.0
NOW = datetime(2026, 10, 8, 14, 5, 30)  # naive local time, as the file name uses
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="needs POSIX file modes and symlinks")


@pytest.fixture(autouse=True)
def clean_log_env(monkeypatch, tmp_path):
    for name in (portlog.ENV_LOG, portlog.ENV_FORMATS, portlog.ENV_DIR, portlog.ENV_TEXT_TIMESTAMPS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SERIAL_MCP_SPEC_ROOT", str(tmp_path / "spec"))


def start_log(tmp_path, formats=("text",)) -> PortLog:
    return PortLog.start(LogRequest(tuple(formats), tmp_path / "logs"), "utf-8", NOW)


class TestResolveLogRequest:
    def test_off_by_default(self):
        assert resolve_log_request(None) is None

    @pytest.mark.parametrize("value", ["1", "true", "YES"])
    def test_environment_enables_logging_in_text_format(self, monkeypatch, value):
        monkeypatch.setenv(portlog.ENV_LOG, value)

        request = resolve_log_request(None)

        assert request is not None
        assert request.formats == ("text",)

    def test_environment_formats_apply_when_logging_is_enabled(self, monkeypatch):
        monkeypatch.setenv(portlog.ENV_LOG, "1")
        monkeypatch.setenv(portlog.ENV_FORMATS, "text, jsonl")

        assert resolve_log_request(None).formats == ("text", "jsonl")

    def test_environment_formats_alone_do_not_enable_logging(self, monkeypatch):
        monkeypatch.setenv(portlog.ENV_FORMATS, "jsonl")
        assert resolve_log_request(None) is None

    def test_false_overrides_the_environment(self, monkeypatch):
        monkeypatch.setenv(portlog.ENV_LOG, "1")
        assert resolve_log_request(False) is None

    def test_false_overrides_formats(self):
        assert resolve_log_request(False, ["jsonl"]) is None

    def test_true_works_with_the_environment_unset(self):
        assert resolve_log_request(True).formats == ("text",)

    def test_formats_alone_enable_logging(self):
        assert resolve_log_request(None, ["jsonl"]).formats == ("jsonl",)

    def test_argument_formats_override_environment_formats(self, monkeypatch):
        monkeypatch.setenv(portlog.ENV_FORMATS, "jsonl")
        assert resolve_log_request(True, ["text"]).formats == ("text",)

    def test_formats_accept_a_comma_separated_string_and_drop_duplicates(self):
        assert resolve_log_request(True, "jsonl, text,jsonl").formats == ("jsonl", "text")

    @pytest.mark.parametrize("bad", [["nope"], [], "", [1], 5, ["text", "xml"]])
    def test_invalid_formats_are_rejected(self, bad):
        with pytest.raises(ValueError, match="log_formats"):
            resolve_log_request(True, bad)

    def test_invalid_environment_formats_name_the_variable(self, monkeypatch):
        monkeypatch.setenv(portlog.ENV_LOG, "1")
        monkeypatch.setenv(portlog.ENV_FORMATS, "nope")
        with pytest.raises(ValueError, match=portlog.ENV_FORMATS):
            resolve_log_request(None)

    def test_directory_defaults_to_logs_under_the_spec_root(self, tmp_path):
        assert resolve_log_request(True).directory == tmp_path / "spec" / "logs"

    def test_directory_comes_from_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv(portlog.ENV_DIR, str(tmp_path / "elsewhere"))
        assert resolve_log_request(True).directory == tmp_path / "elsewhere"


class TestOpenLogFiles:
    def close_all(self, opened):
        for _, fh in opened.values():
            fh.close()

    def test_first_log_has_no_running_number(self, tmp_path):
        opened = open_log_files(tmp_path / "logs", ("text",), NOW)
        self.close_all(opened)

        assert opened["text"][0].name == "serial-mcp-log-08102026-1405.txt"

    def test_second_log_in_the_same_minute_takes_the_next_number(self, tmp_path):
        self.close_all(open_log_files(tmp_path, ("text",), NOW))
        opened = open_log_files(tmp_path, ("text",), NOW)
        self.close_all(opened)

        assert opened["text"][0].name == "serial-mcp-log-08102026-1405-1.txt"

    def test_all_formats_of_one_log_share_the_same_number(self, tmp_path):
        self.close_all(open_log_files(tmp_path, ("jsonl",), NOW))  # takes only the .jsonl name

        opened = open_log_files(tmp_path, ("text", "jsonl"), NOW)
        self.close_all(opened)

        assert sorted(path.name for path, _ in opened.values()) == [
            "serial-mcp-log-08102026-1405-1.jsonl",
            "serial-mcp-log-08102026-1405-1.txt",
        ]

    @POSIX_ONLY
    def test_files_are_private_to_the_owner(self, tmp_path):
        opened = open_log_files(tmp_path, ("text", "jsonl"), NOW)
        self.close_all(opened)

        for path, _ in opened.values():
            assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0

    @POSIX_ONLY
    def test_a_symlink_at_the_next_name_is_never_followed(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("keep me")
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "serial-mcp-log-08102026-1405.txt").symlink_to(outside)

        opened = open_log_files(logs, ("text",), NOW)
        opened["text"][1].write(b"logged")
        self.close_all(opened)

        assert outside.read_text() == "keep me"
        assert opened["text"][0].name == "serial-mcp-log-08102026-1405-1.txt"

    @POSIX_ONLY
    def test_a_dangling_symlink_is_skipped_too(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "serial-mcp-log-08102026-1405.txt").symlink_to(tmp_path / "missing" / "target")

        opened = open_log_files(logs, ("text",), NOW)
        self.close_all(opened)

        assert not (tmp_path / "missing").exists()
        assert opened["text"][0].name.endswith("-1.txt")

    def test_losing_the_name_race_removes_the_files_opened_and_retries(self, tmp_path, monkeypatch):
        real = portlog.open_exclusive
        calls = count()

        def racing(path):
            # The first log's second file is taken by another connection after we opened the first.
            if next(calls) == 1:
                raise FileExistsError(path)
            return real(path)

        monkeypatch.setattr(portlog, "open_exclusive", racing)

        opened = open_log_files(tmp_path, ("text", "jsonl"), NOW)
        self.close_all(opened)

        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "serial-mcp-log-08102026-1405-1.jsonl",
            "serial-mcp-log-08102026-1405-1.txt",
        ]

    def test_creates_missing_parent_directories(self, tmp_path):
        opened = open_log_files(tmp_path / "a" / "b", ("text",), NOW)
        self.close_all(opened)

        assert (tmp_path / "a" / "b").is_dir()


class TestFormats:
    def test_text_log_is_byte_identical_to_received_bytes_and_skips_tx(self, tmp_path):
        log = start_log(tmp_path, ["text"])
        received = [b"U-Boot 2024\r\n", b"\xff\xfe invalid utf-8 \x80", b"\nlast"]
        for chunk in received:
            log.rx(chunk)
        log.tx(b"ls\n", "mcp")
        log.event("dtr", value=False)
        log.close()

        assert log.files["text"].read_bytes() == b"".join(received)
        assert [p.suffix for p in log.files.values()] == [".txt"]

    def test_jsonl_rx_entries_reproduce_the_delivered_bytes_exactly(self, tmp_path):
        log = start_log(tmp_path, ["jsonl"])
        received = [b"abc\xff", b"\xfe\x80def", b"\r\n"]
        for chunk in received:
            log.rx(chunk)
        log.close()

        entries = [e for e in read_jsonl(log.files["jsonl"]) if e.get("dir") == "rx"]
        assert b"".join(bytes.fromhex(e["hex"]) for e in entries) == b"".join(received)
        assert [e["len"] for e in entries] == [4, 5, 2]

    def test_jsonl_tx_entry_carries_source_hex_and_decoded_text(self, tmp_path):
        log = start_log(tmp_path, ["jsonl"])
        log.tx(b"ls\n", "mirror")
        log.close()

        (entry,) = read_jsonl(log.files["jsonl"])
        assert (entry["dir"], entry["source"], entry["len"]) == ("tx", "mirror", 3)
        assert (entry["hex"], entry["text"]) == ("6c730a", "ls\n")
        assert entry["ts"].endswith("Z")

    def test_jsonl_events_carry_their_fields(self, tmp_path):
        log = start_log(tmp_path, ["jsonl"])
        log.event("dtr", value=False)
        log.close()

        (entry,) = read_jsonl(log.files["jsonl"])
        assert (entry["event"], entry["value"]) == ("dtr", False)
        assert "dir" not in entry

    def test_timestamps_follow_the_clocks_and_never_decrease(self, tmp_path):
        ticks = iter(x / 10 for x in range(100))
        wall = iter(datetime(2026, 10, 8, 12, 0, s, tzinfo=UTC) for s in range(60))
        path = tmp_path / "t.jsonl"
        writer = JsonlWriter(open(path, "wb"), WriterOptions("utf-8"))  # noqa: SIM115 - closed by the log
        log = PortLog(
            {"jsonl": writer},
            {"jsonl": path},
            monotonic=lambda: next(ticks),
            wall_clock=lambda: next(wall),
        )
        log.rx(b"a")
        log.tx(b"b", "mcp")
        log.event("flush", what="both")
        log.close()

        entries = read_jsonl(path)
        assert [e["t"] for e in entries] == [0.1, 0.2, 0.3]
        assert entries[0]["ts"] == "2026-10-08T12:00:00.000000Z"

    def test_unknown_connection_encoding_falls_back_to_utf8(self, tmp_path):
        path = tmp_path / "x.jsonl"
        writer = JsonlWriter(open(path, "wb"), WriterOptions("no-such-codec"))  # noqa: SIM115
        log = PortLog({"jsonl": writer}, {"jsonl": path})
        log.rx("é".encode())
        log.close()

        assert read_jsonl(path)[0]["text"] == "é"

    def test_log_is_a_no_op_after_close(self, tmp_path):
        log = start_log(tmp_path, ["text", "jsonl"])
        log.close()
        log.rx(b"late")
        log.tx(b"late", "mcp")
        log.event("late")
        log.close()

        assert log.files["text"].read_bytes() == b""
        assert log.files["jsonl"].read_bytes() == b""

    def test_status_reports_files_formats_and_counts(self, tmp_path):
        log = start_log(tmp_path, ["text", "jsonl"])
        log.rx(b"abc")
        log.close()

        status = log.status()
        assert status["formats"] == ["text", "jsonl"]
        assert status["entries"] == 1
        assert status["dropped_bytes"] == 0
        assert set(status["files"]) == {"text", "jsonl"}

    def test_every_registered_format_has_a_unique_extension(self):
        extensions = [fmt.extension for fmt in FORMATS.values()]
        assert len(extensions) == len(set(extensions))


class GatedWriter:
    """Wraps a format writer and holds its first ``write`` until released."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first = True

    def write(self, entry) -> None:
        if self._first:
            self._first = False
            self.entered.set()
            assert self.release.wait(WAIT_S)
        self._inner.write(entry)

    def flush(self) -> None:
        self._inner.flush()

    def pending_deadline(self):
        return self._inner.pending_deadline()

    def tick(self, offset) -> None:
        self._inner.tick(offset)

    def close(self) -> None:
        self._inner.close()


class TestOverflow:
    def make_stalled_log(self, tmp_path, queue_limit):
        paths = {"text": tmp_path / "o.txt", "jsonl": tmp_path / "o.jsonl"}
        gate = GatedWriter(TextWriter(open(paths["text"], "wb"), WriterOptions("utf-8")))  # noqa: SIM115
        jsonl = JsonlWriter(open(paths["jsonl"], "wb"), WriterOptions("utf-8"))  # noqa: SIM115
        return PortLog({"text": gate, "jsonl": jsonl}, paths, queue_limit=queue_limit), gate, paths

    def test_a_stalled_disk_drops_entries_without_blocking_and_leaves_exact_markers(self, tmp_path):
        log, gate, paths = self.make_stalled_log(tmp_path, queue_limit=10)
        log.rx(b"aaaa")
        assert gate.entered.wait(WAIT_S)  # the disk thread holds the first entry
        log.rx(b"b" * 8)  # queued: 8 of 10
        log.tx(b"x" * 5, "mcp")  # would exceed the limit: dropped
        log.rx(b"c" * 4)  # dropped
        assert log.status()["dropped_bytes"] == 9

        gate.release.set()
        log.rx(b"z")  # accepted; a marker goes in front of it
        log.close()

        assert (
            paths["text"].read_bytes() == b"aaaa" + b"b" * 8 + b"\n--- serial-mcp: 9 bytes not logged ---\nz"
        )
        events = read_jsonl(paths["jsonl"])
        assert [e.get("event") for e in events] == [None, None, "log_overflow", None]
        assert events[2]["dropped_bytes"] == 9
        assert bytes.fromhex(events[3]["hex"]) == b"z"

    def test_drops_still_pending_at_close_are_reported(self, tmp_path):
        log, gate, paths = self.make_stalled_log(tmp_path, queue_limit=4)
        log.rx(b"aaaa")
        assert gate.entered.wait(WAIT_S)
        log.rx(b"bbbb")
        log.rx(b"cc")  # dropped
        gate.release.set()
        log.close()

        assert paths["text"].read_bytes().endswith(b"\n--- serial-mcp: 2 bytes not logged ---\n")
        assert read_jsonl(paths["jsonl"])[-1] == {
            **read_jsonl(paths["jsonl"])[-1],
            "event": "log_overflow",
            "dropped_bytes": 2,
        }

    def test_marker_does_not_add_a_blank_line_after_a_complete_line(self, tmp_path):
        log, gate, paths = self.make_stalled_log(tmp_path, queue_limit=6)
        log.rx(b"done\n")
        assert gate.entered.wait(WAIT_S)
        log.rx(b"x" * 7)  # dropped
        gate.release.set()
        log.close()

        assert paths["text"].read_bytes() == b"done\n--- serial-mcp: 7 bytes not logged ---\n"

    def test_a_failing_format_stops_without_affecting_the_others(self, tmp_path):
        class Broken:
            def write(self, entry):
                raise OSError("disk full")

            def flush(self):
                pass

            def pending_deadline(self):
                return None

            def tick(self, offset):
                pass

            def close(self):
                pass

        path = tmp_path / "ok.txt"
        log = PortLog(
            {"jsonl": Broken(), "text": TextWriter(open(path, "wb"), WriterOptions("utf-8"))},  # noqa: SIM115
            {"jsonl": tmp_path / "x", "text": path},
        )
        log.rx(b"one")
        log.rx(b"two")
        log.close()

        assert path.read_bytes() == b"onetwo"


class TestPortLogSlot:
    def test_taps_do_nothing_without_a_log(self):
        slot = PortLogSlot()
        slot.rx(b"x")
        slot.tx(b"x", "mcp")
        slot.event("dtr", value=True)
        slot.close_log()

    def test_tx_records_only_the_bytes_the_port_accepted(self, tmp_path):
        slot = PortLogSlot()
        slot.set(start_log(tmp_path, ["jsonl"]))
        slot.tx(b"abcdef", "mcp", accepted=2)
        slot.tx(b"zzz", "mcp", accepted=0)
        slot.close_log()

        entries = read_jsonl(next((tmp_path / "logs").glob("*.jsonl")))
        assert [e.get("hex") for e in entries if e.get("dir") == "tx"] == [b"ab".hex()]

    def test_close_log_records_a_close_event_and_detaches(self, tmp_path):
        slot = PortLogSlot()
        slot.set(start_log(tmp_path, ["jsonl"]))

        slot.close_log()

        assert slot.log is None
        assert read_jsonl(next((tmp_path / "logs").glob("*.jsonl")))[-1]["event"] == "close"


class TestEncodings:
    @pytest.mark.parametrize(
        ("name", "valid"),
        [("utf-8", True), ("latin-1", True), ("hex", False), ("nope", False), (5, False), (None, False)],
    )
    def test_only_text_codecs_are_accepted(self, name, valid):
        assert portlog_formats.is_text_encoding(name) is valid

    def test_bytes_to_bytes_codec_falls_back_to_utf8_instead_of_dropping_the_log(self, tmp_path):
        path = tmp_path / "x.jsonl"
        log = PortLog({"jsonl": JsonlWriter(open(path, "wb"), WriterOptions("hex"))}, {"jsonl": path})  # noqa: SIM115
        log.rx(b"ok")
        log.close()

        assert read_jsonl(path)[0]["text"] == "ok"

    def test_configure_event_switches_the_decoding_of_later_entries(self, tmp_path):
        path = tmp_path / "x.jsonl"
        log = PortLog({"jsonl": JsonlWriter(open(path, "wb"), WriterOptions("utf-8"))}, {"jsonl": path})  # noqa: SIM115
        log.rx("é".encode("latin-1"))
        log.event("configure", encoding="latin-1")
        log.rx("é".encode("latin-1"))
        log.close()

        rx = [e for e in read_jsonl(path) if e.get("dir") == "rx"]
        assert [e["text"] for e in rx] == ["�", "é"]
        assert [e["hex"] for e in rx] == ["e9", "e9"]
