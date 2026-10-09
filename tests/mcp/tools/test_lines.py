"""Tests for the control line tools: set and pulse DTR and RTS."""

from __future__ import annotations

from serial_mcp_server.mcp.tools.lines import (
    HANDLERS,
    TOOLS,
    handle_pulse_dtr,
    handle_pulse_rts,
    handle_set_dtr,
    handle_set_rts,
)


class TestToolRegistration:
    def test_every_tool_has_a_handler(self):
        assert {t.name for t in TOOLS} == set(HANDLERS)

    def test_tools_in_registration_order(self):
        assert [t.name for t in TOOLS] == [
            "serial.set_dtr",
            "serial.set_rts",
            "serial.pulse_dtr",
            "serial.pulse_rts",
        ]


class TestControlLines:
    async def test_set_dtr(self, connected_entry):
        state, _conn = connected_entry
        result = await handle_set_dtr(state, {"connection_id": "s1", "value": False})
        assert result["ok"]
        assert result["dtr"] is False

    async def test_set_rts(self, connected_entry):
        state, _conn = connected_entry
        result = await handle_set_rts(state, {"connection_id": "s1", "value": True})
        assert result["ok"]
        assert result["rts"] is True

    async def test_pulse_dtr(self, connected_entry):
        state, conn = connected_entry
        result = await handle_pulse_dtr(state, {"connection_id": "s1", "duration_ms": 10})
        assert result["ok"]
        assert result["duration_ms"] == 10
        # DTR should end high (True) after pulse
        assert conn.ser.dtr is True

    async def test_pulse_rts(self, connected_entry):
        state, conn = connected_entry
        result = await handle_pulse_rts(state, {"connection_id": "s1", "duration_ms": 10})
        assert result["ok"]
        assert result["duration_ms"] == 10
        assert conn.ser.rts is True

    async def test_pulse_dtr_clamped(self, connected_entry):
        state, _conn = connected_entry
        result = await handle_pulse_dtr(state, {"connection_id": "s1", "duration_ms": 999999})
        assert result["ok"]
        assert result["duration_ms"] == 10000
