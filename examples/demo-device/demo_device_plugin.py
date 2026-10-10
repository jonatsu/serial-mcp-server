"""Plugin for DemoDevice device."""

import functools
import json
import math
from collections.abc import Awaitable, Callable

from serial_mcp_server.plugin_api import (
    SerialState,
    Tool,
    ToolAnnotations,
    err,
    handle_read_until,
    handle_write,
    ok,
)

META = {
    "description": "DemoDevice device plugin — version, status, config, echo, ping, sampling, logging, auth",
    "device_name_contains": "DemoDevice",
}

PROMPT = "> "
SAMPLE_MARGIN_MS = 2000  # Slack on top of the sampling time itself.
SAMPLE_LINE_BYTES = 80  # Generous size of one "[SAMPLE] n/count ..." line, to size the read.


class _CommandFailed(Exception):
    """A step of a device command failed. ``result`` is the error result to give the agent."""

    def __init__(self, result: dict) -> None:
        super().__init__(result["error"]["message"])
        self.result = result


async def _send_cmd(
    state: SerialState, connection_id: str, cmd: str, timeout_ms: int = 2000, max_bytes: int = 4096
) -> str:
    """Send a command and read until the '> ' prompt. Returns the response text (without prompt).

    Raises ``_CommandFailed`` when the write or the read fails, or when the prompt does not arrive.
    """
    written = await handle_write(
        state,
        {
            "connection_id": connection_id,
            "data": cmd,
            "append_newline": True,
        },
    )
    if not written["ok"]:
        raise _CommandFailed(written)
    resp = await handle_read_until(
        state,
        {
            "connection_id": connection_id,
            "delimiter": PROMPT,
            "timeout_ms": timeout_ms,
            "max_bytes": max_bytes,
        },
    )
    if not resp["ok"]:
        raise _CommandFailed(resp)
    text = resp.get("data", "")
    if not text.endswith(PROMPT):
        # A timeout or a full read returns what arrived so far, as a success.
        raise _CommandFailed(
            err(
                "timeout",
                f"No '{PROMPT}' prompt after '{cmd}' within {timeout_ms} ms. Reply so far: {text!r}",
            )
        )
    return text[: -len(PROMPT)].strip()


def _reporting_failures(
    handler: Callable[[SerialState, dict], Awaitable[dict]],
) -> Callable[[SerialState, dict], Awaitable[dict]]:
    """Return the error result of a failed command instead of raising."""

    @functools.wraps(handler)
    async def wrapper(state: SerialState, args: dict) -> dict:
        try:
            return await handler(state, args)
        except _CommandFailed as exc:
            return exc.result

    return wrapper


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="demo.version",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description=(
            "Use to read the DemoDevice firmware version. Returns version, such as 'DemoDevice v1.0.0'. "
            "Changes nothing on the device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="demo.status",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description=(
            "Use to see what the device is doing. Returns state ('idle', 'logging', or 'sampling'), temp, "
            "uptime in seconds, logs_enabled, and authenticated. Changes nothing on the device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="demo.ping",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description=(
            "Use to check that the device answers. Returns response, which is 'pong' from a working device. "
            "Changes nothing on the device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="demo.echo",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description=(
            "Use to test that text makes the round trip to the device and back. Returns echo, the text the "
            "device sent back. Changes nothing on the device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "text": {"type": "string", "description": "Text to echo."},
            },
            "required": ["connection_id", "text"],
        },
    ),
    Tool(
        name="demo.uptime",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description=(
            "Use to read how long the device has run since its last boot or reset. Returns uptime as text, "
            "such as '1234s'. Changes nothing on the device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="demo.config_get",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description=(
            "Use to read the device configuration, for example before demo.config_set. Returns "
            "log_interval_ms, sample_rate_hz, and device_name, or only the one value named by key. An unknown "
            "key returns the device's error text as raw. Changes nothing on the device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "key": {
                    "type": "string",
                    "description": "Optional config key (log_interval_ms, sample_rate_hz, device_name).",
                },
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="demo.config_set",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
        description=(
            "Use to change a device setting: log_interval_ms (100-10000), sample_rate_hz (1-100), or "
            "device_name. Returns response, 'OK <key>=<value>' when the device accepts the value, or the "
            "device's 'ERROR: ...' text when it rejects the key or value. Overwrites the stored value; a "
            "reboot keeps it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "key": {"type": "string", "description": "Config key to set."},
                "value": {"type": "string", "description": "Value to set."},
            },
            "required": ["connection_id", "key", "value"],
        },
    ),
    Tool(
        name="demo.sample",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
        description=(
            "Use to collect a fixed number of sensor readings. Waits until the device finishes, which takes "
            "count divided by sample_rate_hz seconds (100 seconds for 1000 samples at the default 10 Hz). "
            "Returns raw, the device's text with one '[SAMPLE] n/count temp=... humidity=...' line per "
            "reading and a final '[SAMPLE] DONE'. Puts the device in the 'sampling' state until it is done."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Number of samples to collect, 1 to 1000.",
                },
            },
            "required": ["connection_id", "count"],
        },
    ),
    Tool(
        name="demo.log_start",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
        description=(
            "Use to make the device print a '[LOG]' line at a fixed interval. Returns response, 'OK logs "
            "started (interval=<n>ms)', or an 'ERROR: ...' text for an interval outside 100-10000 ms. Logging "
            "continues until demo.log_stop or a reboot, and log lines can appear in the output of later "
            "commands. A given interval_ms also replaces the stored log_interval_ms."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "interval_ms": {
                    "type": "integer",
                    "description": "Log interval in ms, 100 to 10000 (default from device config).",
                },
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="demo.log_stop",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        ),
        description=(
            "Use to stop the periodic logging that demo.log_start began. Returns response, 'OK logs stopped'. "
            "Changes nothing when logging is already off."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="demo.auth",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        ),
        description=(
            "Use before demo.secret, to unlock the privileged commands. Returns response, 'OK authenticated' "
            "for the right password or 'ERROR: wrong password'. Keeps the session authenticated until the "
            "next reboot."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "password": {"type": "string", "description": "Authentication password."},
            },
            "required": ["connection_id", "password"],
        },
    ),
    Tool(
        name="demo.secret",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        description=(
            "Use to read the device's secret value after demo.auth succeeded. Returns secret, which is "
            "'ERROR: not authenticated' when the session is not authenticated. Changes nothing on the device."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
            },
            "required": ["connection_id"],
        },
    ),
    Tool(
        name="demo.reboot",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
        ),
        description=(
            "Use to restart the device, for example to clear a stuck state. Takes about one second and "
            "returns response, the 'Rebooting...' line and the boot banner. Stops logging and sampling and "
            "ends the authentication; the configuration is kept."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
            },
            "required": ["connection_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_version(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], "version")
    return ok(version=text)


async def handle_status(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], "status")
    try:
        data = json.loads(text)
        return ok(**data)
    except json.JSONDecodeError:
        return ok(raw=text)


async def handle_ping(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], "ping")
    return ok(response=text)


async def handle_echo(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], f"echo {args['text']}")
    return ok(echo=text)


async def handle_uptime(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], "uptime")
    return ok(uptime=text)


async def handle_config_get(state: SerialState, args: dict) -> dict:
    key = args.get("key", "")
    cmd = f"config get {key}".strip()
    text = await _send_cmd(state, args["connection_id"], cmd)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return ok(**data)
        return ok(value=data)
    except json.JSONDecodeError:
        return ok(raw=text)


async def handle_config_set(state: SerialState, args: dict) -> dict:
    cmd = f"config set {args['key']} {args['value']}"
    text = await _send_cmd(state, args["connection_id"], cmd)
    return ok(response=text)


async def _sample_rate_hz(state: SerialState, connection_id: str) -> int:
    """Read the device's sample rate. Falls back to its slowest rate, 1 Hz, so a timeout built on it is long enough."""
    text = await _send_cmd(state, connection_id, "config get sample_rate_hz")
    try:
        return max(1, int(json.loads(text)["sample_rate_hz"]))
    except (ValueError, KeyError, TypeError):
        return 1


async def handle_sample(state: SerialState, args: dict) -> dict:
    connection_id = args["connection_id"]
    count = int(args["count"])
    # The device sends one line per 1/rate seconds and the prompt only after the last, so the wait grows with
    # the count and shrinks with the rate. The read also needs room for every sample line.
    rate_hz = await _sample_rate_hz(state, connection_id)
    timeout_ms = math.ceil(count / rate_hz * 1000) + SAMPLE_MARGIN_MS
    text = await _send_cmd(
        state,
        connection_id,
        f"sample {count}",
        timeout_ms=timeout_ms,
        max_bytes=count * SAMPLE_LINE_BYTES + 256,
    )
    return ok(raw=text)


async def handle_log_start(state: SerialState, args: dict) -> dict:
    interval = args.get("interval_ms")
    cmd = f"log start {interval}" if interval else "log start"
    text = await _send_cmd(state, args["connection_id"], cmd)
    return ok(response=text)


async def handle_log_stop(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], "log stop")
    return ok(response=text)


async def handle_auth(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], f"auth {args['password']}")
    return ok(response=text)


async def handle_secret(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], "secret")
    return ok(secret=text)


async def handle_reboot(state: SerialState, args: dict) -> dict:
    text = await _send_cmd(state, args["connection_id"], "reboot")
    return ok(response=text)


HANDLERS = {
    name: _reporting_failures(handler)
    for name, handler in {
        "demo.version": handle_version,
        "demo.status": handle_status,
        "demo.ping": handle_ping,
        "demo.echo": handle_echo,
        "demo.uptime": handle_uptime,
        "demo.config_get": handle_config_get,
        "demo.config_set": handle_config_set,
        "demo.sample": handle_sample,
        "demo.log_start": handle_log_start,
        "demo.log_stop": handle_log_stop,
        "demo.auth": handle_auth,
        "demo.secret": handle_secret,
        "demo.reboot": handle_reboot,
    }.items()
}
