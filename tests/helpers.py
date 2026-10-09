"""Helpers shared by test modules."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from serial_mcp_server.mcp.state import SerialState


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSON Lines file into a list of objects."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def writer_goes_idle(writer: Any, source: str, within_seconds: float) -> bool:
    """Poll until *writer* has no queued or running bytes from *source*; False if *within_seconds* pass first.

    Blocking, so call it with ``asyncio.to_thread`` from async tests.
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
