"""Helpers shared by test modules."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import anyio.abc

from serial_mcp_server.mcp.state import SerialState


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSON Lines file into a list of objects."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def writer_goes_idle(writer: Any, source: str, within_seconds: float) -> bool:
    """Poll until *writer* has no queued or running bytes from *source*; False if *within_seconds* pass first.

    Blocking, so call it with ``anyio.to_thread.run_sync`` from async tests.
    """
    deadline = time.monotonic() + within_seconds
    while writer.pending_bytes(source):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.002)
    return True


def reconfigure(state: SerialState, **changes: Any) -> None:
    """Replace some of *state*'s settings, as a server started with those values would have them."""
    state.settings = state.settings.model_copy(update=changes)


@contextmanager
def skipped_timed_waits() -> Iterator[list[float]]:
    """Make every timed ``threading.Event.wait`` return False at once, and record its timeout.

    Write pacing sleeps with ``cancel.wait(delay)``, so this runs a paced write without its delays and
    shows the delays it asked for. A wait without a timeout still blocks: starting a thread, which
    ``anyio.to_thread.run_sync`` does, waits on an event until the thread runs.
    """
    timeouts: list[float] = []
    real_wait = threading.Event.wait

    def wait(event: threading.Event, timeout: float | None = None) -> bool:
        if timeout is None:
            return real_wait(event)
        timeouts.append(timeout)
        return False

    with patch.object(threading.Event, "wait", wait):
        yield timeouts


@dataclass
class Request:
    """A tool call running in a task group, inside its own cancel scope as the MCP SDK runs a request."""

    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    finished: anyio.Event = field(default_factory=anyio.Event)
    result: Any = None
    error: Exception | None = None

    def cancel(self) -> None:
        """Cancel the call the way the SDK cancels a request: through its cancel scope."""
        self.scope.cancel()

    async def outcome(self) -> Any:
        """Wait for the call to end; return its result, or raise the exception it raised."""
        await self.finished.wait()
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def cancelled(self) -> bool:
        """True when the call ended because it was cancelled."""
        return self.scope.cancelled_caught


def start_request(tg: anyio.abc.TaskGroup, handler: Callable[..., Awaitable[Any]], *args: Any) -> Request:
    """Start ``handler(*args)`` in *tg* and return its :class:`Request`."""
    request = Request()

    async def run() -> None:
        try:
            with request.scope:
                request.result = await handler(*args)
        except Exception as exc:  # handed to outcome(), so the test sees it where it waits
            request.error = exc
        finally:
            request.finished.set()

    tg.start_soon(run)
    return request
