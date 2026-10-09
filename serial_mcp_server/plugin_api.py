"""What a plugin may import from the server: response builders, the state type, and the serial tool handlers.

Plugins import only from this module. A name in ``__all__`` changes only with a CHANGELOG entry, so a plugin
that follows this rule keeps working when the server's own modules move.

Call the serial handlers to reach the device rather than the port object: the server's reader thread owns
reads, and its writer thread queues and paces every write.
"""

from __future__ import annotations

from mcp.types import Tool

from serial_mcp_server.mcp.responses import _err as err
from serial_mcp_server.mcp.responses import _ok as ok
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.tools.io import handle_read, handle_read_until, handle_readline, handle_write

__all__ = [
    "SerialState",
    "Tool",
    "err",
    "handle_read",
    "handle_read_until",
    "handle_readline",
    "handle_write",
    "ok",
]
