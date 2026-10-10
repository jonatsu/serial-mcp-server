"""Unit tests for server-level helpers and build_server wiring."""

import json

from mcp.types import ListToolsRequest

from serial_mcp_server.mcp.responses import _err, _ok, _result_text

EXPECTED_TOOL_HINTS = {
    "serial.list_ports": {"readOnlyHint": True, "openWorldHint": True},
    "serial.open": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.close": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.connection_status": {"readOnlyHint": True, "openWorldHint": False},
    "serial.log.start": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    "serial.log.stop": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    "serial.log.status": {"readOnlyHint": True, "openWorldHint": False},
    "serial.log.mark": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    "serial.configure": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "serial.read": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.write": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.readline": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.read_until": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.flush": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.send_file": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.set_dtr": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "serial.set_rts": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "serial.pulse_dtr": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.pulse_rts": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.connections.list": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.template": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.register": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "serial.spec.list": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.attach": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "serial.spec.get": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.read": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.search": {"readOnlyHint": True, "openWorldHint": False},
    "serial.trace.status": {"readOnlyHint": True, "openWorldHint": False},
    "serial.trace.tail": {"readOnlyHint": True, "openWorldHint": False},
    "serial.plugin.list": {"readOnlyHint": True, "openWorldHint": False},
    "serial.plugin.reload": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.plugin.template": {"readOnlyHint": True, "openWorldHint": False},
    "serial.plugin.load": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.script.run": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.script.status": {"readOnlyHint": True, "openWorldHint": False},
    "serial.script.cancel": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    "serial.script.list": {"readOnlyHint": True, "openWorldHint": False},
    "serial.script.template": {"readOnlyHint": True, "openWorldHint": False},
}

# ---------------------------------------------------------------------------
# Result format tests
# ---------------------------------------------------------------------------


class TestResultFormat:
    def test_ok_shape(self):
        r = _ok(foo="bar")
        assert r == {"ok": True, "foo": "bar"}

    def test_err_shape(self):
        r = _err("some_code", "some message")
        assert r == {"ok": False, "error": {"code": "some_code", "message": "some message"}}

    def test_result_text_is_json(self):
        payload = {"ok": True, "x": 1}
        texts = _result_text(payload)
        assert len(texts) == 1
        parsed = json.loads(texts[0].text)
        assert parsed == payload


# ---------------------------------------------------------------------------
# build_server wiring
# ---------------------------------------------------------------------------


class TestBuildServer:
    def test_build_server_returns_server_and_state(self, settings):
        from serial_mcp_server.mcp.server import build_server

        server, state = build_server(settings)
        assert server is not None
        assert state is not None
        assert hasattr(state, "connections")

    async def test_client_visible_tools_have_expected_annotations(self, settings):
        from serial_mcp_server.mcp.server import build_server

        server, _state = build_server(settings)
        result = await server.request_handlers[ListToolsRequest](None)
        tools = {tool.name: tool for tool in result.root.tools}

        assert set(tools) == set(EXPECTED_TOOL_HINTS)
        for name, expected in EXPECTED_TOOL_HINTS.items():
            assert tools[name].annotations is not None
            actual = tools[name].annotations.model_dump(exclude_none=True)
            assert actual.items() >= expected.items()

    async def test_tool_annotations_are_serialized_for_mcp_clients(self, settings):
        from serial_mcp_server.mcp.server import build_server

        server, _state = build_server(settings)
        result = await server.request_handlers[ListToolsRequest](None)
        tools = {tool.name: tool for tool in result.root.tools}

        serialized = json.loads(tools["serial.write"].model_dump_json(exclude_none=True))

        assert serialized["annotations"]["readOnlyHint"] is False
        assert serialized["annotations"]["destructiveHint"] is True
        assert serialized["annotations"]["openWorldHint"] is True

    async def test_another_separator_renames_the_tools_their_text_names(self, settings):
        from serial_mcp_server.mcp.server import build_server

        server, _state = build_server(settings.model_copy(update={"tool_separator": "_"}))
        result = await server.request_handlers[ListToolsRequest](None)
        tools = result.root.tools
        dotted = {name for name in EXPECTED_TOOL_HINTS}

        assert {tool.name for tool in tools} == {name.replace(".", "_") for name in dotted}
        for tool in tools:
            texts = [tool.description or ""]
            texts += [prop.get("description", "") for prop in tool.inputSchema.get("properties", {}).values()]
            for text in texts:
                assert not [name for name in dotted if name in text], (tool.name, text)
        assert "serial_spec_list" in next(
            tool.description or "" for tool in tools if tool.name == "serial_open"
        )

    async def test_another_separator_leaves_the_default_tool_list_unchanged(self, settings):
        from serial_mcp_server.mcp.server import build_server

        build_server(settings.model_copy(update={"tool_separator": "_"}))
        server, _state = build_server(settings)
        result = await server.request_handlers[ListToolsRequest](None)

        assert {tool.name for tool in result.root.tools} == set(EXPECTED_TOOL_HINTS)
        assert "serial.spec.list" in next(
            tool.description or "" for tool in result.root.tools if tool.name == "serial.open"
        )

    async def test_another_separator_renames_the_tools_an_error_message_names(self, settings):
        from mcp.types import CallToolRequest, CallToolRequestParams

        from serial_mcp_server.mcp.server import build_server

        server, _state = build_server(settings.model_copy(update={"tool_separator": "_"}))
        request = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name="serial_close", arguments={"connection_id": "x"}),
        )
        result = await server.request_handlers[CallToolRequest](request)
        message = json.loads(result.root.content[0].text)["error"]["message"]

        assert "serial_connections_list" in message
        assert "serial.connections.list" not in message
