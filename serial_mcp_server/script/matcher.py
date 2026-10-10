"""Thread-safe decoded output window for a script run."""

from __future__ import annotations

import codecs
import threading
import time
from dataclasses import dataclass

# Most unconsumed output, in decoded characters, that a run keeps for ``@expect`` and ``@prompt``. When output
# arrives faster than the script consumes it, the oldest characters are dropped, an earlier match can be
# lost, and the count appears as ``window_overflow`` in the result. ``@discard`` or a matching ``@expect``
# frees room.
_MAX_WINDOW_CHARS = 1_048_576
# Characters of the previous check's text that the next ``@fail_on`` check searches again, so a match split
# across two checks is still seen. A match longer than this that straddles two checks can be missed.
FAIL_PATTERN_OVERLAP_CHARS = 256


@dataclass(frozen=True)
class OutputSnapshot:
    """Unconsumed text and its revision, used to reject stale cursor updates."""

    revision: int
    text: str


class OutputMatcher:
    """Store decoded output while keeping pattern evaluation on the runner thread.

    Bytes are decoded with the connection's encoding as they arrive; an invalid sequence becomes U+FFFD, and a
    character split between two chunks is held until it completes. Two views of the same output exist: the
    window that ``@expect`` and ``@prompt`` consume from, which ``@discard`` empties, and a separate feed for
    ``@fail_on`` that consumption never removes.
    """

    def __init__(self, encoding: str) -> None:
        self._decoder: codecs.IncrementalDecoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self._condition: threading.Condition = threading.Condition()
        self._window: str = ""
        self._revision: int = 0
        self._dropped_chars: int = 0
        self._new_output: str = ""
        self._fail_overlap: str = ""
        self._output_generation: int = 0
        self._observed_generation: int = 0
        self._interrupt_generation: int = 0
        self._observed_interrupt_generation: int = 0
        self._closed: bool = False
        self._error: Exception | None = None

    def feed(self, data: bytes) -> None:
        """Decode and append received bytes, then wake waiters without evaluating patterns."""
        if not data:
            return
        with self._condition:
            if self._closed:
                return
            text = self._decoder.decode(data)
            if not text:
                return
            self._append(text)
            self._new_output += text
            self._output_generation += 1
            self._condition.notify_all()

    def snapshot(self) -> OutputSnapshot:
        """Return the current unconsumed text and mark its output generation as observed."""
        with self._condition:
            self._observed_generation = self._output_generation
            return OutputSnapshot(self._revision, self._window)

    def consume(self, snapshot: OutputSnapshot, end: int) -> bool:
        """Consume through *end* in *snapshot*, or reject it if the window changed first."""
        with self._condition:
            if snapshot.revision != self._revision:
                return False
            if not 0 <= end <= len(snapshot.text):
                raise ValueError("match end is outside the output snapshot")
            if not self._window.startswith(snapshot.text[:end]):
                return False
            if end:
                self._window = self._window[end:]
                self._revision += 1
            return True

    def take_since_fail_check(self) -> str:
        """Return new output with the 256-character overlap from the previous check."""
        with self._condition:
            if not self._new_output:
                return ""
            text = self._fail_overlap + self._new_output
            self._fail_overlap = text[-FAIL_PATTERN_OVERLAP_CHARS:]
            self._new_output = ""
            return text

    def discard(self) -> None:
        """Discard unconsumed match text without removing output from fail-pattern checks."""
        with self._condition:
            if self._window:
                self._window = ""
                self._revision += 1

    def wait(self, deadline: float) -> bool:
        """Wait until new output, close, interruption, or an absolute monotonic deadline."""
        with self._condition:
            while True:
                if self._closed or self._output_generation > self._observed_generation:
                    return True
                if self._interrupt_generation > self._observed_interrupt_generation:
                    self._observed_interrupt_generation = self._interrupt_generation
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _ = self._condition.wait(timeout=remaining)

    def interrupt(self) -> None:
        """Wake one or more waiters without adding output."""
        with self._condition:
            self._interrupt_generation += 1
            self._condition.notify_all()

    def close(self, error: Exception | None = None) -> None:
        """Mark end of stream, flush an incomplete encoded character, and wake waiters."""
        with self._condition:
            if self._closed:
                return
            text = self._decoder.decode(b"", final=True)
            if text:
                self._append(text)
                self._new_output += text
                self._output_generation += 1
            self._closed = True
            self._error = error
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def error(self) -> Exception | None:
        with self._condition:
            return self._error

    @property
    def dropped_chars(self) -> int:
        with self._condition:
            return self._dropped_chars

    def _append(self, text: str) -> None:
        self._window += text
        excess = len(self._window) - _MAX_WINDOW_CHARS
        if excess > 0:
            self._window = self._window[excess:]
            self._dropped_chars += excess
            self._revision += 1
