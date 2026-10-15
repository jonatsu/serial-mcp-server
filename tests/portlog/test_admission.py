"""Admission limits protect serial threads even when a log writer stalls."""

from __future__ import annotations

import threading

import pytest

from serial_mcp_server.portlog.formats import JsonlWriter, WriterOptions
from serial_mcp_server.portlog.log import PortLog
from tests.helpers import read_jsonl

WAIT_S = 2.0


class GatedJsonlWriter(JsonlWriter):
    def __init__(self, fh):
        super().__init__(fh, WriterOptions())
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, entry):
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(WAIT_S)
        super().write(entry)


@pytest.fixture
def stalled_log(tmp_path):
    logs = []

    def make(*, first_event=None, **limits):
        path = tmp_path / f"{len(logs)}.jsonl"
        writer = GatedJsonlWriter(path.open("wb"))
        log = PortLog({"jsonl": writer}, {"jsonl": path}, **limits)
        logs.append((log, writer))
        if first_event is None:
            log.rx(b"a")
        else:
            log.event("mark", label=first_event)
        assert writer.entered.wait(WAIT_S)
        return log, writer, path

    yield make
    for log, writer in logs:
        writer.release.set()
        log.close()


def test_payload_in_flight_counts_against_admission(stalled_log):
    log, writer, path = stalled_log(queue_limit=4)
    log.rx(b"bbbb")
    assert log.status()["dropped_bytes"] == 4
    writer.release.set()
    log.close()
    assert [record.get("hex") for record in read_jsonl(path)] == ["61", None]


@pytest.mark.parametrize("kind", ["empty", "tiny", "event"])
def test_entry_count_bounds_empty_data_tiny_data_and_events(stalled_log, kind):
    log, writer, path = stalled_log(entry_limit=3)
    for _ in range(20):
        if kind == "event":
            log.event("mark", label="x")
        else:
            log.rx(b"" if kind == "empty" else b"x")
    status = log.status()
    assert status["dropped_entries"] == 18
    assert status["dropped_events"] == (18 if kind == "event" else 0)
    assert status["dropped_bytes"] == (18 if kind == "tiny" else 0)
    writer.release.set()
    log.close()
    records = read_jsonl(path)
    assert len(records) == 4
    assert records[-1]["dropped_entries"] == 18


def test_retained_metadata_budget_includes_in_flight_event(stalled_log):
    log, writer, path = stalled_log(metadata_limit=200, first_event="x" * 100)
    assert not log.event("mark", label="y" * 100)
    writer.release.set()
    log.close()
    assert [r.get("label") for r in read_jsonl(path) if r.get("event") == "mark"] == ["x" * 100]
    assert log.status()["dropped_events"] == 1


def test_normal_overflow_markers_consume_entry_budget(stalled_log):
    log, writer, path = stalled_log(entry_limit=2)
    log.rx(b"x" * (9 * 1024 * 1024))
    # There is room for a new event alone, but not its required preceding loss marker as well.
    assert not log.event("mark", label="no room for both")
    writer.release.set()
    log.close()
    records = read_jsonl(path)
    assert [record.get("event") for record in records] == [None, "log_overflow"]
    assert records[-1]["dropped_entries"] == 2


def test_large_source_metadata_rejects_complete_payload(stalled_log):
    log, writer, path = stalled_log()
    log.tx(b"\x00\xff\r\n", "x" * (1024 * 1024))
    writer.release.set()
    log.close()
    records = read_jsonl(path)
    assert log.status()["dropped_bytes"] == 4
    assert log.status()["dropped_events"] == 0
    assert [record.get("hex") for record in records] == ["61", None]


def test_metadata_is_private_before_caller_mutates_it(stalled_log):
    log, writer, path = stalled_log()
    values = [{"value": "before"}]
    log.event("mark", values=values)
    values[0]["value"] = "after"
    values.append({"value": "new"})
    writer.release.set()
    log.close()
    assert read_jsonl(path)[1]["values"] == [{"value": "before"}]


@pytest.mark.parametrize("bad", ["huge", "deep", "wide", "cycle", "object", "integer"])
def test_unsupported_and_oversized_metadata_is_rejected_whole(stalled_log, bad):
    log, writer, path = stalled_log()
    value = {
        "huge": "x" * (2 * 1024 * 1024),
        "deep": [[[[[["deep"]]]]]],
        "wide": list(range(129)),
        "object": object(),
        "integer": 1 << 1_000_000,
    }.get(bad)
    if bad == "cycle":
        value = []
        value.append(value)
    assert not log.event("mark", value=value)
    assert log.event("mark", label="accepted")
    writer.release.set()
    log.close()
    records = read_jsonl(path)
    assert [r.get("event") for r in records] == [None, "log_overflow", "mark"]
    assert records[1]["dropped_entries"] == records[1]["dropped_events"] == 1
    assert records[2]["label"] == "accepted"


def test_encoding_event_rejection_stops_admission_and_drains_prefix(stalled_log, caplog):
    log, writer, path = stalled_log(entry_limit=2)
    log.rx(b"before")
    assert not log.event("configure", encoding="latin-1")
    log.rx(b"\xe9")
    log.event("configure", encoding="utf-8")
    writer.release.set()
    log.close()
    records = read_jsonl(path)
    assert [r.get("event") for r in records] == [None, None, "log_overflow", "log_admission_failed"]
    assert [r["hex"] for r in records if "hex" in r] == ["61", b"before".hex()]
    assert log.status()["admission_error"]
    assert len([r for r in caplog.records if "admission" in r.message]) == 1


def test_repeated_close_events_do_not_grow_reserved_shutdown_entries(stalled_log):
    log, writer, path = stalled_log(entry_limit=1)
    for _ in range(100):
        log.event("mark", label="dropped")
    for _ in range(100):
        log.event("close")
    writer.release.set()
    log.close()
    records = read_jsonl(path)
    assert [r.get("event") for r in records] == [None, "log_overflow", "close"]
    assert records[1]["dropped_events"] == 100
