"""Synchronous script execution and result recording."""

from __future__ import annotations

import threading
import time
from _thread import LockType
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn

import regex

from serial_mcp_server.config import DEFAULT_SCRIPT_MAX_SECONDS, DEFAULT_SCRIPT_PATTERN_SECONDS
from serial_mcp_server.port.pacing import Pacing
from serial_mcp_server.port.writer import WriteAborted, WriterClosed
from serial_mcp_server.script.directives import LINE_NAMES
from serial_mcp_server.script.matcher import OutputMatcher
from serial_mcp_server.script.model import Script, Step, StepKind
from serial_mcp_server.script.results import (
    RunFailure,
    RunMetadata,
    RunResult,
    RunState,
    RunStatus,
    StepResult,
    open_result_file,
    utc_now,
    write_result_file,
)
from serial_mcp_server.script.session import PortSession

SCRIPT_CANCEL_WAIT_SECONDS = 5.0  # how long a cancel waits for the runner thread
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_OUTPUT_TAIL_CHARS = 2048
_FAIL_PATTERN_OVERLAP_CHARS = 256


@dataclass(frozen=True)
class _ScriptPattern:
    """A compiled script pattern and the script line that defined it.

    Messages name the line and never the pattern: ``@expect``, ``@prompt``, and ``@fail_on`` patterns hold
    expanded variable values, and a result must not repeat those.
    """

    compiled: regex.Pattern[str]
    line: int


def _script_pattern(step: Step) -> _ScriptPattern:
    return _ScriptPattern(regex.compile(step.payload), step.line)


@dataclass
class _RunSettings:
    """Mutable settings scoped to one script run."""

    pacing: Pacing | None = None
    newline: str = ""
    timeout: float = _DEFAULT_TIMEOUT_SECONDS
    prompt: _ScriptPattern | None = None


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
    pattern: _ScriptPattern
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
    """Execute a parsed script synchronously on a dedicated daemon thread.

    Each pattern search stops at *pattern_timeout_seconds* and releases the interpreter lock while it runs,
    so a pattern that backtracks without end ends its own run and never freezes the server.
    """

    def __init__(
        self,
        script: Script,
        session: PortSession,
        metadata: RunMetadata,
        pattern_timeout_seconds: float = DEFAULT_SCRIPT_PATTERN_SECONDS,
        *,
        max_seconds: float = DEFAULT_SCRIPT_MAX_SECONDS,
    ) -> None:
        self._script: Script = script
        self._session: PortSession = session
        self._metadata: RunMetadata = metadata
        self._pattern_timeout_seconds: float = pattern_timeout_seconds
        self._max_seconds: float = max_seconds
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
        """Request cancellation and wake a matcher wait immediately; a search in flight ends at its limit."""
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

    def _search(self, pattern: _ScriptPattern, text: str) -> regex.Match[str] | None:
        limit = self._pattern_timeout_seconds
        try:
            # concurrent=True releases the interpreter lock, so the event loop, readers, and cancel keep running.
            return pattern.compiled.search(text, timeout=limit, concurrent=True)
        except TimeoutError:
            self._check_stop()  # a cancel that arrived during the search outranks the search's failure
            raise _RunTermination(
                RunState.ERRORED,
                "regex_timeout",
                detail=(
                    f"The pattern on line {pattern.line} ran longer than {limit:g} s, so the run stopped. "
                    "Rewrite it without nested quantifiers."
                ),
            ) from None

    def _check_fail_patterns(self, text: str) -> None:
        if not text:
            return
        for active in self._fail_patterns:
            searchable = text[active.skip_chars :]
            active.skip_chars = 0
            match = self._search(active.pattern, searchable)
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
        pattern: _ScriptPattern | None = None,
    ) -> str | None:
        while True:
            self._check_stop()
            snapshot = self._matcher.snapshot()
            self._check_fail_patterns(self._matcher.take_since_fail_check())

            if pattern is not None:
                match = self._search(pattern, snapshot.text)
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
                    _script_pattern(step),
                )
            case StepKind.PROMPT:
                settings.prompt = _script_pattern(step) if step.enabled else None
            case StepKind.FAIL_ON:
                if step.enabled:
                    self._fail_patterns.append(
                        _ActiveFailPattern(_script_pattern(step), self._fail_overlap_length)
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

        ended_at = utc_now().astimezone(UTC)
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
        started_at = utc_now().astimezone(UTC)
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

    def _write_result(self, result: RunResult) -> None:
        path, file_handle = open_result_file(self._metadata, result.started_at)
        with self._lock:
            self._result_path = path
        write_result_file(file_handle, result)
