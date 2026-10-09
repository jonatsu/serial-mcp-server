"""Synchronous script execution and result recording."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from _thread import LockType
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal, NoReturn

from serial_mcp_server.portlog import (
    DIR_MODE,
    FILE_MODE,
    MAX_NAME_COLLISIONS,
    log_base_name,
    open_exclusive,
)
from serial_mcp_server.script.directives import LINE_NAMES
from serial_mcp_server.script.matcher import OutputMatcher
from serial_mcp_server.script.model import Script, Step, StepKind
from serial_mcp_server.script.session import PortSession
from serial_mcp_server.writer import Pacing, WriteAborted, WriterClosed

_DEFAULT_MAX_SECONDS = 3600.0
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_OUTPUT_TAIL_CHARS = 2048
_FAIL_PATTERN_OVERLAP_CHARS = 256
_MAX_SECONDS_ENV = "SERIAL_MCP_SCRIPT_MAX_SECONDS"
_RESULT_PREFIX = "serial-mcp-script"
_RESULT_SCHEMA_VERSION = 1


def _utc_now() -> datetime:
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


@dataclass
class _RunSettings:
    """Mutable settings scoped to one script run."""

    pacing: Pacing | None = None
    newline: str = ""
    timeout: float = _DEFAULT_TIMEOUT_SECONDS
    prompt: re.Pattern[str] | None = None


@dataclass
class _RunExecution:
    """Mutable progress accumulated while the run thread executes and finalizes."""

    state: RunState = RunState.PASSED
    reason: str | None = None
    failure: RunFailure | None = None
    steps: list[StepResult] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)
    current_step: Step | None = None


@dataclass
class _ActiveFailPattern:
    pattern: re.Pattern[str]
    skip_chars: int = 0


class _RunTermination(Exception):
    state: RunState
    reason: str
    matched_text: str | None
    detail: str | None

    def __init__(
        self,
        state: RunState,
        reason: str,
        *,
        matched_text: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or reason)
        self.state = state
        self.reason = reason
        self.matched_text = matched_text
        self.detail = detail


class ScriptRunner:
    """Execute a parsed script synchronously on a dedicated daemon thread."""

    def __init__(self, script: Script, session: PortSession, metadata: RunMetadata) -> None:
        self._script: Script = script
        self._session: PortSession = session
        self._metadata: RunMetadata = metadata
        self._max_seconds: float = self._read_max_seconds()
        self._matcher: OutputMatcher = OutputMatcher(session.encoding)
        self._stop_event: threading.Event = threading.Event()
        self._done: threading.Event = threading.Event()
        self._lock: LockType = threading.Lock()
        self._state: RunState = RunState.RUNNING
        self._current_line: int | None = None
        self._current_text: str | None = None
        self._completed_steps: int = 0
        self._reason: str | None = None
        self._result: RunResult | None = None
        self._result_path: Path | None = metadata.result_path
        self._thread: threading.Thread | None = None
        self._stop_cause: _RunTermination | None = None
        self._run_started_monotonic: float = 0.0
        self._run_deadline: float = 0.0
        self._fail_patterns: list[_ActiveFailPattern] = []
        self._fail_overlap_length: int = 0

    @staticmethod
    def _read_max_seconds() -> float:
        raw = os.environ.get(_MAX_SECONDS_ENV)
        if raw is None:
            return _DEFAULT_MAX_SECONDS
        try:
            seconds = float(raw)
        except ValueError as exc:
            raise ValueError(f"{_MAX_SECONDS_ENV} must be a finite positive number of seconds.") from exc
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"{_MAX_SECONDS_ENV} must be a finite positive number of seconds.")
        return seconds

    @property
    def done(self) -> threading.Event:
        """An event set once the runner has finalized its result."""
        return self._done

    @property
    def result(self) -> RunResult | None:
        """The completed result, or ``None`` while the run is active."""
        with self._lock:
            return self._result

    @property
    def result_path(self) -> Path | None:
        """The configured result path or the collision-safe path selected at completion."""
        with self._lock:
            return self._result_path

    def start(self) -> None:
        """Start the script on its own daemon thread."""
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("ScriptRunner.start() may only be called once.")
            thread = threading.Thread(target=self._run, name="serial-script-runner", daemon=True)
            self._thread = thread
        try:
            thread.start()
        except BaseException:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
            raise

    def cancel(self) -> None:
        """Request cancellation and wake a matcher wait immediately."""
        self._request_stop(_RunTermination(RunState.CANCELLED, "cancelled"))

    def join(self, timeout: float | None = None) -> None:
        """Wait up to *timeout* seconds for the runner thread to finish."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def snapshot(self) -> RunStatus:
        """Return current status without waiting for execution or I/O."""
        with self._lock:
            return RunStatus(
                state=self._state,
                current_line=self._current_line,
                reason=self._reason,
                current_text=self._current_text,
                variables=self._script.variable_names,
                completed_steps=self._completed_steps,
            )

    def _request_stop(self, cause: _RunTermination) -> None:
        with self._lock:
            if self._done.is_set() or self._stop_cause is not None:
                return
            self._stop_cause = cause
            self._stop_event.set()
        self._matcher.interrupt()

    def _stop_requested(self) -> _RunTermination | None:
        with self._lock:
            return self._stop_cause

    def _set_current_step(self, step: Step) -> None:
        with self._lock:
            self._current_line = step.line
            self._current_text = step.text

    def _check_stop(self) -> None:
        cause = self._stop_requested()
        if cause is not None:
            raise cause

    def _check_fail_patterns(self, text: str) -> None:
        if not text:
            return
        for active in self._fail_patterns:
            searchable = text[active.skip_chars :]
            active.skip_chars = 0
            match = active.pattern.search(searchable)
            if match is not None:
                cause = _RunTermination(
                    RunState.FAILED,
                    "fail_pattern",
                    matched_text=match.group(0),
                )
                self._request_stop(cause)
                raise cause
        self._fail_overlap_length = min(len(text), _FAIL_PATTERN_OVERLAP_CHARS)

    def _check_runtime(self) -> None:
        self._check_stop()
        _ = self._matcher.snapshot()
        self._check_fail_patterns(self._matcher.take_since_fail_check())
        if self._matcher.closed:
            error = self._matcher.error
            raise _RunTermination(
                RunState.ERRORED,
                "end_of_stream",
                detail=str(error) if error is not None else None,
            )
        if time.monotonic() >= self._run_deadline:
            raise _RunTermination(RunState.FAILED, "max_duration")

    def _wait_until(
        self,
        deadline: float,
        pattern: re.Pattern[str] | None = None,
    ) -> str | None:
        while True:
            self._check_stop()
            snapshot = self._matcher.snapshot()
            self._check_fail_patterns(self._matcher.take_since_fail_check())

            if pattern is not None:
                match = pattern.search(snapshot.text)
                if match is not None:
                    if self._matcher.consume(snapshot, match.end()):
                        return match.group(0)
                    continue

            if self._matcher.closed:
                error = self._matcher.error
                raise _RunTermination(
                    RunState.ERRORED,
                    "end_of_stream",
                    detail=str(error) if error is not None else None,
                )

            now = time.monotonic()
            if now >= self._run_deadline:
                raise _RunTermination(RunState.FAILED, "max_duration")
            if now >= deadline:
                if pattern is None:
                    return None
                raise _RunTermination(RunState.FAILED, "timeout")
            _ = self._matcher.wait(min(deadline, self._run_deadline))

    def _termination_for_exception(
        self,
        error: Exception,
        *,
        default_reason: str = "serial_error",
        allow_cancellation: bool = True,
        preserve_internal: bool = True,
        classify_file_errors: bool = False,
    ) -> _RunTermination:
        if preserve_internal and isinstance(error, _RunTermination):
            return error
        if allow_cancellation and isinstance(error, WriterClosed):
            return _RunTermination(RunState.CANCELLED, "writer_closed", detail=str(error))
        if allow_cancellation and isinstance(error, WriteAborted) and self._stop_event.is_set():
            cause = self._stop_requested()
            if cause is not None:
                return cause
            return _RunTermination(RunState.CANCELLED, "cancelled", detail=str(error))
        if classify_file_errors and isinstance(
            error,
            (FileNotFoundError, PermissionError, IsADirectoryError),
        ):
            default_reason = "file_error"
        return _RunTermination(RunState.ERRORED, default_reason, detail=str(error))

    def _raise_termination(
        self,
        error: Exception,
        *,
        default_reason: str = "serial_error",
        classify_file_errors: bool = False,
    ) -> NoReturn:
        termination = self._termination_for_exception(
            error,
            default_reason=default_reason,
            classify_file_errors=classify_file_errors,
        )
        if termination is error:
            raise termination
        raise termination from error

    def _write(self, data: bytes, pacing: Pacing | None) -> None:
        try:
            accepted = self._session.write(data, pacing, self._stop_event)
        except Exception as exc:
            self._raise_termination(exc)
        if accepted != len(data):
            raise _RunTermination(
                RunState.ERRORED,
                "serial_error",
                detail=f"write accepted {accepted} of {len(data)} bytes.",
            )

    def _send_file(self, path: Path, pacing: Pacing | None) -> None:
        try:
            expected = path.stat().st_size
        except Exception as exc:
            self._raise_termination(exc, default_reason="file_error")
        try:
            accepted = self._session.send_file(path, pacing, self._stop_event)
        except Exception as exc:
            self._raise_termination(exc, classify_file_errors=True)
        if accepted != expected:
            raise _RunTermination(
                RunState.ERRORED,
                "serial_error",
                detail=f"file send accepted {accepted} of {expected} bytes.",
            )

    @staticmethod
    def _line_name(value: str) -> Literal["dtr", "rts"]:
        if value not in LINE_NAMES:
            raise _RunTermination(
                RunState.ERRORED,
                "serial_error",
                detail=f"unsupported control line {value!r}.",
            )
        return value

    def _execute_step(
        self,
        step: Step,
        settings: _RunSettings,
        marks: list[str],
    ) -> str | None:
        matched_text: str | None = None
        match step.kind:
            case StepKind.BARE:
                data = (step.payload + settings.newline).encode(self._session.encoding)
                self._write(data, settings.pacing)
                if settings.prompt is not None:
                    _ = self._wait_until(time.monotonic() + settings.timeout, settings.prompt)
            case StepKind.SEND:
                self._write(step.payload.encode(self._session.encoding), settings.pacing)
            case StepKind.SENDLINE:
                data = (step.payload + settings.newline).encode(self._session.encoding)
                self._write(data, settings.pacing)
                if settings.prompt is not None:
                    _ = self._wait_until(time.monotonic() + settings.timeout, settings.prompt)
            case StepKind.SENDHEX:
                self._write(step.data, settings.pacing)
            case StepKind.EXPECT:
                seconds = step.seconds if step.seconds is not None else settings.timeout
                matched_text = self._wait_until(
                    time.monotonic() + seconds,
                    re.compile(step.payload),
                )
            case StepKind.PROMPT:
                settings.prompt = re.compile(step.payload) if step.enabled else None
            case StepKind.FAIL_ON:
                if step.enabled:
                    self._fail_patterns.append(
                        _ActiveFailPattern(re.compile(step.payload), self._fail_overlap_length)
                    )
                else:
                    self._fail_patterns.clear()
            case StepKind.TIMEOUT:
                settings.timeout = step.seconds if step.seconds is not None else settings.timeout
            case StepKind.SLEEP:
                seconds = step.seconds if step.seconds is not None else 0.0
                _ = self._wait_until(time.monotonic() + seconds)
            case StepKind.DISCARD:
                self._matcher.discard()
            case StepKind.LINE:
                line_name = self._line_name(step.line_name)
                self._session.set_line(line_name, step.enabled)
            case StepKind.PULSE:
                line_name = self._line_name(step.line_name)
                self._session.set_line(line_name, False)
                try:
                    seconds = step.seconds if step.seconds is not None else 0.0
                    _ = self._wait_until(time.monotonic() + seconds)
                finally:
                    self._session.set_line(line_name, True)
            case StepKind.PACING:
                settings.pacing = step.pacing
            case StepKind.NEWLINE:
                settings.newline = step.payload
            case StepKind.MARK:
                self._session.log_event("mark", label=step.payload)
                marks.append(step.payload)
            case StepKind.SEND_FILE:
                if step.path is None:
                    raise _RunTermination(
                        RunState.ERRORED,
                        "file_error",
                        detail="@send_file has no resolved path.",
                    )
                self._send_file(step.path, settings.pacing)
        return matched_text

    def _make_failure(
        self,
        termination: _RunTermination,
        step: Step | None,
    ) -> RunFailure:
        tail = self._matcher.snapshot().text[-_MAX_OUTPUT_TAIL_CHARS:]
        return RunFailure(
            line=step.line if step is not None else None,
            text=step.text if step is not None else None,
            reason=termination.reason,
            output_tail=tail,
            matched_text=termination.matched_text,
            detail=termination.detail,
        )

    def _record_termination(
        self,
        execution: _RunExecution,
        termination: _RunTermination,
        step: Step | None,
    ) -> None:
        execution.state = termination.state
        execution.reason = termination.reason
        execution.failure = self._make_failure(termination, step)

    def _execute_steps(self, execution: _RunExecution, settings: _RunSettings) -> None:
        for step in self._script.steps:
            execution.current_step = step
            self._set_current_step(step)
            step_started = time.monotonic()
            matched_text: str | None = None
            termination: _RunTermination | None = None
            try:
                self._check_runtime()
                matched_text = self._execute_step(step, settings, execution.marks)
                self._check_runtime()
            except Exception as exc:
                termination = self._termination_for_exception(exc)

            step_ended = time.monotonic()
            if termination is not None:
                self._record_termination(execution, termination, step)
            execution.steps.append(
                StepResult(
                    line=step.line,
                    text=step.text,
                    kind=step.kind,
                    start_offset=step_started - self._run_started_monotonic,
                    duration=step_ended - step_started,
                    outcome=termination.reason if termination is not None else "passed",
                    matched_text=matched_text,
                )
            )
            with self._lock:
                self._completed_steps += 1
            if termination is not None:
                break

    def _promote_passed_failure(
        self,
        execution: _RunExecution,
        error: Exception,
        *,
        reason: str,
    ) -> None:
        if execution.state is not RunState.PASSED:
            return
        termination = self._termination_for_exception(
            error,
            default_reason=reason,
            allow_cancellation=False,
            preserve_internal=False,
        )
        self._record_termination(execution, termination, execution.current_step)

    def _build_result(
        self,
        execution: _RunExecution,
        log_paths: tuple[str, ...],
        started_at: datetime,
        ended_at: datetime,
    ) -> RunResult:
        return RunResult(
            script=self._metadata.script_name,
            sha256=self._script.sha256,
            variables=self._script.variable_names,
            connection=dict(self._metadata.connection_config),
            started_at=started_at,
            ended_at=ended_at,
            state=execution.state,
            reason=execution.reason,
            steps=tuple(execution.steps),
            marks=tuple(execution.marks),
            failure=execution.failure,
            log_paths=log_paths,
            window_overflow=self._matcher.dropped_chars,
        )

    def _finalize_run(
        self,
        execution: _RunExecution,
        *,
        started_at: datetime,
        start_event_logged: bool,
        unsubscribe: Callable[[], None] | None,
    ) -> RunResult:
        if start_event_logged:
            try:
                self._session.log_event(
                    "script_end",
                    state=execution.state.value,
                    reason=execution.reason,
                )
            except Exception as exc:
                self._promote_passed_failure(execution, exc, reason="serial_error")

        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception as exc:
                self._promote_passed_failure(execution, exc, reason="serial_error")

        try:
            log_paths = tuple(str(path) for path in self._metadata.log_paths())
        except Exception as exc:
            log_paths = ()
            self._promote_passed_failure(execution, exc, reason="file_error")

        ended_at = _utc_now().astimezone(UTC)
        result = self._build_result(execution, log_paths, started_at, ended_at)
        try:
            self._write_result(result)
        except Exception as exc:
            termination = self._termination_for_exception(
                exc,
                default_reason="file_error",
                allow_cancellation=False,
                preserve_internal=False,
            )
            self._record_termination(execution, termination, execution.current_step)
            result = self._build_result(execution, log_paths, started_at, ended_at)
        return result

    def _run(self) -> None:
        started_at = _utc_now().astimezone(UTC)
        self._run_started_monotonic = time.monotonic()
        self._run_deadline = self._run_started_monotonic + self._max_seconds
        execution = _RunExecution()
        unsubscribe: Callable[[], None] | None = None
        start_event_logged = False

        try:
            self._check_runtime()
            unsubscribe = self._session.subscribe(self._matcher)
            self._session.log_event(
                "script_start",
                script=self._metadata.script_name,
                sha256=self._script.sha256,
            )
            start_event_logged = True
            self._check_runtime()
            settings = _RunSettings(newline=self._session.newline)
            self._execute_steps(execution, settings)
        except Exception as exc:
            termination = self._termination_for_exception(exc)
            self._record_termination(execution, termination, execution.current_step)

        result = self._finalize_run(
            execution,
            started_at=started_at,
            start_event_logged=start_event_logged,
            unsubscribe=unsubscribe,
        )
        with self._lock:
            self._state = result.state
            self._current_line = None
            self._current_text = None
            self._reason = result.reason
            self._result = result
        self._done.set()

    def _open_result(self, started_at: datetime) -> tuple[Path, BinaryIO]:
        if self._metadata.result_path is not None:
            path = self._metadata.result_path
            path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            file_handle = open_exclusive(path)
            with self._lock:
                self._result_path = path
            return path, file_handle

        directory = self._metadata.result_directory
        directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        timestamp = started_at.astimezone()
        for number in range(MAX_NAME_COLLISIONS):
            base = log_base_name(_RESULT_PREFIX, timestamp, number)
            path = directory / f"{base}.json"
            try:
                file_handle = open_exclusive(path)
            except FileExistsError:
                continue
            with self._lock:
                self._result_path = path
            return path, file_handle
        raise OSError(f"No free script result filename in {directory} after {MAX_NAME_COLLISIONS} tries.")

    def _write_result(self, result: RunResult) -> None:
        _, file_handle = self._open_result(result.started_at)
        with file_handle:
            if hasattr(os, "fchmod"):
                os.fchmod(file_handle.fileno(), FILE_MODE)
            encoded = json.dumps(result.to_json(), ensure_ascii=False, indent=2).encode("utf-8")
            _ = file_handle.write(encoded)
            _ = file_handle.write(b"\n")
