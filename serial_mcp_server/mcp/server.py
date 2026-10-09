"""Serial MCP server – stdio transport, stateful serial tools via pyserial."""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

import anyio
import serial as pyserial
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from serial_mcp_server import config
from serial_mcp_server.config import ConfigError, Settings
from serial_mcp_server.mcp.plugins import PluginManager
from serial_mcp_server.mcp.responses import _err, _result_text
from serial_mcp_server.mcp.state import SerialState
from serial_mcp_server.mcp.tools import connection as connection_tools
from serial_mcp_server.mcp.tools import file as file_tools
from serial_mcp_server.mcp.tools import introspection as introspection_tools
from serial_mcp_server.mcp.tools import io as io_tools
from serial_mcp_server.mcp.tools import lines as lines_tools
from serial_mcp_server.mcp.tools import log as log_tools
from serial_mcp_server.mcp.tools import plugin as plugin_tools
from serial_mcp_server.mcp.tools import script as script_tools
from serial_mcp_server.mcp.tools import spec as spec_tools
from serial_mcp_server.mcp.tools import trace as trace_tools
from serial_mcp_server.mcp.trace import get_trace_buffer, init_trace

logger = logging.getLogger("serial_mcp_server")

# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def _apply_tool_separator(tools: list[Tool], handlers: dict[str, Any], sep: str) -> None:
    """Replace '.' with *sep* in every tool name and handler key."""
    if sep == ".":
        return
    for t in tools:
        t.name = t.name.replace(".", sep)
    for old_key in list(handlers):
        new_key = old_key.replace(".", sep)
        if new_key != old_key:
            handlers[new_key] = handlers.pop(old_key)


def build_server(settings: Settings) -> tuple[Server, SerialState]:
    state = SerialState(settings)
    server = Server("serial-mcp-server")

    tools: list[Tool] = (
        connection_tools.TOOLS
        + io_tools.TOOLS
        + lines_tools.TOOLS
        + introspection_tools.TOOLS
        + file_tools.TOOLS
        + log_tools.TOOLS
        + script_tools.TOOLS
        + spec_tools.TOOLS
        + trace_tools.TOOLS
        + plugin_tools.TOOLS
    )
    handlers: dict[str, Any] = {
        **connection_tools.HANDLERS,
        **io_tools.HANDLERS,
        **lines_tools.HANDLERS,
        **introspection_tools.HANDLERS,
        **file_tools.HANDLERS,
        **log_tools.HANDLERS,
        **script_tools.HANDLERS,
        **spec_tools.HANDLERS,
        **trace_tools.HANDLERS,
    }

    # --- Plugin system ---
    manager = PluginManager(
        settings.data_root / "plugins",
        tools,
        handlers,
        enabled=settings.plugins.enabled,
        allowlist=settings.plugins.allowlist,
        tool_separator=settings.tool_separator,
    )
    manager.load_all()
    handlers.update(plugin_tools.make_handlers(manager, server))

    # Rename tool names / handler keys for clients that reject dots.
    _apply_tool_separator(tools, handlers, settings.tool_separator)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        arguments = arguments or {}

        buf = get_trace_buffer()
        if buf:
            cid = arguments.get("connection_id")
            safe_args = buf.sanitize(arguments)
            buf.emit({"event": "tool_call_start", "tool": name, "args": safe_args, "connection_id": cid})
            t0 = time.monotonic()

        handler = handlers.get(name)
        if handler is None:
            return _result_text(_err("unknown_tool", f"No tool named {name}"))

        try:
            result = await handler(state, arguments)
        except KeyError as exc:
            result = _err("not_found", str(exc))
        except (ValueError, TypeError) as exc:
            result = _err("invalid_params", str(exc))
        except RuntimeError as exc:
            result = _err("limit_reached", str(exc))
        except pyserial.SerialException as exc:
            result = _err("io_error", str(exc))
        except ConnectionError as exc:
            result = _err("disconnected", str(exc))
        except TimeoutError:
            result = _err("timeout", "Serial operation timed out.")
        except Exception as exc:
            logger.error("Unhandled error in %s: %s", name, exc, exc_info=True)
            result = _err("internal", f"Internal error in {name}. Check server logs for details.")

        if result.get("ok") and "connection_id" in arguments:
            conn = state.connections.get(arguments["connection_id"])
            if conn:
                conn.last_seen_ts = time.time()

        if buf:
            duration_ms = round((time.monotonic() - t0) * 1000, 1)
            buf.emit(
                {
                    "event": "tool_call_end",
                    "tool": name,
                    "ok": result.get("ok"),
                    "error_code": result.get("error", {}).get("code")
                    if isinstance(result.get("error"), dict)
                    else None,
                    "duration_ms": duration_ms,
                    "connection_id": cid,
                }
            )

        return _result_text(result)

    init_trace(settings)
    return server, state


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# How long the server waits for connections to stop when it exits; their threads finish on their own.
SHUTDOWN_LIMIT_SECONDS = 0.25


async def _run(settings: Settings) -> None:
    server, state = build_server(settings)

    logger.info("Starting Serial MCP server (max_connections=%s)", settings.max_connections)

    _BENIGN_ASYNC = (EOFError, BrokenPipeError, anyio.ClosedResourceError, anyio.BrokenResourceError)

    try:
        async with stdio_server() as (read_stream, write_stream):
            init_options = server.create_initialization_options(
                notification_options=NotificationOptions(tools_changed=True),
            )
            await server.run(read_stream, write_stream, init_options)
    except _BENIGN_ASYNC:
        # Normal termination — client closed stdin / streams broke.
        pass
    except BaseExceptionGroup as eg:
        # anyio wraps stream-closure errors in ExceptionGroup on Python 3.11+.
        if not all(isinstance(e, _BENIGN_ASYNC) for e in eg.exceptions):
            raise
    finally:
        try:
            # Shielded from the cancellation that may be ending the server; the deadline still applies.
            with anyio.move_on_after(SHUTDOWN_LIMIT_SECONDS, shield=True):
                await state.shutdown()
        except Exception:
            pass
        buf = get_trace_buffer()
        if buf:
            try:
                buf.close()
            except Exception:
                pass


_BENIGN_SYNC = (
    KeyboardInterrupt,
    BrokenPipeError,
    EOFError,
    ConnectionError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
)


CONFIG_ERROR_EXIT_STATUS = 2


def main() -> None:
    try:
        settings = config.load()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(CONFIG_ERROR_EXIT_STATUS)
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        anyio.run(_run, settings)
    except _BENIGN_SYNC:
        pass
    except BaseExceptionGroup as eg:
        if not all(isinstance(e, _BENIGN_SYNC) for e in eg.exceptions):
            raise


if __name__ == "__main__":
    main()
