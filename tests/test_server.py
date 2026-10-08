"""Unit tests for server-level helpers and build_server wiring."""

import json

from mcp.types import ListToolsRequest

from serial_mcp_server.helpers import _err, _ok, _result_text

EXPECTED_TOOL_HINTS = {
    "serial.list_ports": {"readOnlyHint": True, "openWorldHint": True},
    "serial.open": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.close": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.connection_status": {"readOnlyHint": True, "openWorldHint": False},
    "serial.configure": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    "serial.read": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.write": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.readline": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.read_until": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.flush": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.set_dtr": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.set_rts": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.pulse_dtr": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.pulse_rts": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.connections.list": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.template": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.register": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    "serial.spec.list": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.attach": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    "serial.spec.get": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.read": {"readOnlyHint": True, "openWorldHint": False},
    "serial.spec.search": {"readOnlyHint": True, "openWorldHint": False},
    "serial.trace.status": {"readOnlyHint": True, "openWorldHint": False},
    "serial.trace.tail": {"readOnlyHint": True, "openWorldHint": False},
    "serial.plugin.list": {"readOnlyHint": True, "openWorldHint": False},
    "serial.plugin.reload": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    "serial.plugin.template": {"readOnlyHint": True, "openWorldHint": False},
    "serial.plugin.load": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
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
    def test_build_server_returns_server_and_state(self):
        from serial_mcp_server.server import build_server

        server, state = build_server()
        assert server is not None
        assert state is not None
        assert hasattr(state, "connections")

    async def test_client_visible_tools_have_expected_annotations(self):
        from serial_mcp_server.server import build_server

        server, _state = build_server()
        result = await server.request_handlers[ListToolsRequest](None)
        tools = {tool.name: tool for tool in result.root.tools}

        assert set(tools) == set(EXPECTED_TOOL_HINTS)
        for name, expected in EXPECTED_TOOL_HINTS.items():
            assert tools[name].annotations is not None
            actual = tools[name].annotations.model_dump(exclude_none=True)
            assert actual.items() >= expected.items()

    async def test_tool_annotations_are_serialized_for_mcp_clients(self):
        from serial_mcp_server.server import build_server

        server, _state = build_server()
        result = await server.request_handlers[ListToolsRequest](None)
        tools = {tool.name: tool for tool in result.root.tools}

        serialized = json.loads(tools["serial.write"].model_dump_json(exclude_none=True))

        assert serialized["annotations"]["readOnlyHint"] is False
        assert serialized["annotations"]["destructiveHint"] is True
        assert serialized["annotations"]["openWorldHint"] is True
