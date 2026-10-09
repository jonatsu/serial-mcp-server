"""Response builders and shared checks for handler modules."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from serial_mcp_server.state import SerialState

from mcp.types import TextContent

# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> bool:
    """Coerce a value to bool, handling string representations."""
    if isinstance(value, str):
        return value.lower() not in ("false", "0", "")
    return bool(value)


def _ok(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, **kwargs}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def script_running_error(state: SerialState, connection_id: str) -> dict[str, Any] | None:
    """Reject a mutating tool when a script owns this connection."""
    run = state.script_jobs.active_for_connection(connection_id)
    if run is None:
        return None
    return _err("script_running", f"Script run {run.run_id} is running on this connection.")


def _result_text(payload: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, default=str))]
