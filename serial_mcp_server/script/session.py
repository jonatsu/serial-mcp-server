"""Synchronous port capabilities used by the MCP and command-line adapters."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from serial_mcp_server.port.pacing import Pacing
from serial_mcp_server.script.matcher import OutputMatcher


class PortSession(Protocol):
    """The engine's only route to a serial connection."""

    newline: str
    encoding: str

    def write(
        self,
        data: bytes,
        pacing: Pacing | None,
        cancel: threading.Event,
        *,
        check_runtime: Callable[[], None] | None = None,
    ) -> int:
        """Write bytes, polling *check_runtime* on the caller's thread while waiting.

        The checkpoint signals *cancel* instead of raising. Wait for the write to settle before returning
        or raising, so the caller keeps ownership of the connection until writing stops.
        """
        ...

    def send_file(
        self,
        path: Path,
        pacing: Pacing | None,
        cancel: threading.Event,
        *,
        check_runtime: Callable[[], None] | None = None,
    ) -> int:
        """Send a confined file, using the same checkpoint and settlement contract as ``write``."""
        ...

    def set_line(self, line: Literal["dtr", "rts"], value: bool) -> None:
        """Set a serial control line."""
        ...

    def log_event(self, name: str, **fields: Any) -> None:
        """Record a port-log event when logging is active."""
        ...

    def subscribe(self, matcher: OutputMatcher) -> Callable[[], None]:
        """Subscribe a matcher to received bytes and return its unsubscriber."""
        ...
