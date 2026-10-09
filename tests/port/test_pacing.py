"""Tests for serial_mcp_server.port.pacing — pacing estimates."""

from __future__ import annotations

import pytest

from serial_mcp_server.port.pacing import NO_PACING, Pacing


class TestPacingEstimate:
    @pytest.mark.parametrize(
        ("pacing", "size", "seconds"),
        [
            (NO_PACING, 1000, 0.0),
            (Pacing(inter_char_delay_ms=8), 7500, 60.0),
            (Pacing(chunk_size=4, chunk_delay_ms=10), 10, 0.03),
            (Pacing(chunk_size=4, chunk_delay_ms=10), 0, 0.0),
        ],
    )
    def test_estimate_is_segments_times_delay(self, pacing, size, seconds):
        assert pacing.estimated_seconds(size) == pytest.approx(seconds)

    def test_character_pacing_is_chunk_pacing_of_one(self):
        pacing = Pacing(inter_char_delay_ms=8)
        assert (pacing.segment_size, pacing.segment_delay_ms) == (1, 8)
