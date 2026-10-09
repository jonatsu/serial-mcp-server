"""Write pacing: how a payload is split and delayed, and how long a transfer takes on the wire."""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_PACING_DELAY_MS = 1000  # longest pause between segments that a pacing setting may ask for
BITS_PER_BYTE_ON_WIRE = 10  # start bit, 8 data bits, stop bit


@dataclass(frozen=True)
class Pacing:
    """How a payload is split and delayed on its way to the port."""

    inter_char_delay_ms: int = 0
    chunk_size: int = 0
    chunk_delay_ms: int = 0

    @property
    def is_paced(self) -> bool:
        return bool(self.inter_char_delay_ms or self.chunk_size)

    @property
    def segment_size(self) -> int:
        """Bytes per write; character pacing is chunk pacing with a chunk of 1. Zero when unpaced."""
        return 1 if self.inter_char_delay_ms else self.chunk_size

    @property
    def segment_delay_ms(self) -> int:
        return self.inter_char_delay_ms or self.chunk_delay_ms

    def estimated_seconds(self, payload_size: int) -> float:
        """Upper-bound pacing time for *payload_size* bytes: segments times the delay."""
        if not self.is_paced:
            return 0.0
        segments = math.ceil(payload_size / self.segment_size)
        return segments * self.segment_delay_ms / 1000.0


NO_PACING = Pacing()


NO_PACING = Pacing()


def wire_seconds(size: int, baudrate: int) -> float:
    """Time *size* bytes take on the wire at *baudrate*, with ten bits per byte."""
    return size * BITS_PER_BYTE_ON_WIRE / baudrate if baudrate > 0 else 0.0


def paced_transfer_seconds(pacing: Pacing, size: int, baudrate: int) -> float:
    """Estimated time of a paced transfer: the larger of the pauses between segments and the time on the wire.

    The larger of the two, because large chunks make the pauses negligible while the line stays busy.
    Zero when unpaced, because the cap limits pacing, not the speed of the line.
    """
    if not pacing.is_paced:
        return 0.0
    return max(pacing.estimated_seconds(size), wire_seconds(size, baudrate))
