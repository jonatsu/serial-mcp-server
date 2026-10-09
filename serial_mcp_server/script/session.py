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

    def write(self, data: bytes, pacing: Pacing | None, cancel: threading.Event) -> int:
        """Write bytes and return the amount accepted by the port."""
        ...

    def send_file(self, path: Path, pacing: Pacing | None, cancel: threading.Event) -> int:
        """Send a confined file and return the amount accepted by the port."""
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
