"""A script run's states, metadata, step records, and result, and the result file that records it."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from serial_mcp_server.private_files import (
    DIR_MODE,
    FILE_MODE,
    MAX_NAME_COLLISIONS,
    log_base_name,
    open_exclusive,
)
from serial_mcp_server.script.model import StepKind

RESULT_PREFIX = "serial-mcp-script"
_RESULT_SCHEMA_VERSION = 1


def utc_now() -> datetime:
    """Return the current aware UTC time."""
    return datetime.now(UTC)


class RunState(StrEnum):
    """A script run's current or terminal state."""

    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunMetadata:
    """Information needed to execute a script and record its result."""

    script_name: str
    connection_config: Mapping[str, object]
    result_directory: Path
    log_paths: Callable[[], tuple[Path, ...]]
    result_path: Path | None = None


@dataclass(frozen=True)
class RunStatus:
    """A nonblocking snapshot of the runner's current status."""

    state: RunState
    current_line: int | None
    reason: str | None = None
    current_text: str | None = None
    variables: tuple[str, ...] = ()
    completed_steps: int = 0


@dataclass(frozen=True)
class StepResult:
    """The recorded outcome for one executed script step."""

    line: int
    text: str
    kind: StepKind
    start_offset: float
    duration: float
    outcome: str
    matched_text: str | None = None

    def to_json(self) -> dict[str, object]:
        """Serialize this step with the stable result-file field names."""
        return {
            "line": self.line,
            "text": self.text,
            "kind": self.kind.value,
            "start_offset": self.start_offset,
            "duration": self.duration,
            "outcome": self.outcome,
            "matched_text": self.matched_text,
        }


@dataclass(frozen=True)
class RunFailure:
    """Failure or cancellation details, including unmatched device output."""

    line: int | None
    text: str | None
    reason: str
    output_tail: str
    matched_text: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, object]:
        """Serialize the terminal failure details."""
        return {
            "line": self.line,
            "text": self.text,
            "reason": self.reason,
            "output_tail": self.output_tail,
            "matched_text": self.matched_text,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RunResult:
    """A complete script-run result and its JSON representation."""

    script: str
    sha256: str
    variables: tuple[str, ...]
    connection: Mapping[str, object]
    started_at: datetime
    ended_at: datetime
    state: RunState
    reason: str | None
    steps: tuple[StepResult, ...]
    marks: tuple[str, ...]
    failure: RunFailure | None
    log_paths: tuple[str, ...]
    window_overflow: int = 0
    schema_version: int = _RESULT_SCHEMA_VERSION

    def to_json(self) -> dict[str, object]:
        """Return the design's versioned, JSON-compatible result structure."""
        return {
            "schema_version": self.schema_version,
            "script": self.script,
            "sha256": self.sha256,
            "variables": list(self.variables),
            "connection": dict(self.connection),
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "state": self.state.value,
            "reason": self.reason,
            "steps": [step.to_json() for step in self.steps],
            "marks": list(self.marks),
            "failure": self.failure.to_json() if self.failure is not None else None,
            "log_paths": list(self.log_paths),
            "window_overflow": self.window_overflow,
        }


def open_result_file(metadata: RunMetadata, started_at: datetime) -> tuple[Path, BinaryIO]:
    """Create the run's result file: ``metadata.result_path`` if set, else a new name in the result directory.

    Raises:
        OSError: The file cannot be created, or every name up to the limit is taken.
    """
    if metadata.result_path is not None:
        path = metadata.result_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        return path, open_exclusive(path)

    directory = metadata.result_directory
    directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    timestamp = started_at.astimezone()
    for number in range(MAX_NAME_COLLISIONS):
        base = log_base_name(RESULT_PREFIX, timestamp, number)
        path = directory / f"{base}.json"
        try:
            return path, open_exclusive(path)
        except FileExistsError:
            continue
    raise OSError(f"No free script result filename in {directory} after {MAX_NAME_COLLISIONS} tries.")


def write_result_file(file_handle: BinaryIO, result: RunResult) -> None:
    """Write *result* as JSON into *file_handle*, owner-only, and close it."""
    with file_handle:
        if hasattr(os, "fchmod"):
            os.fchmod(file_handle.fileno(), FILE_MODE)
        encoded = json.dumps(result.to_json(), ensure_ascii=False, indent=2).encode("utf-8")
        _ = file_handle.write(encoded)
        _ = file_handle.write(b"\n")
