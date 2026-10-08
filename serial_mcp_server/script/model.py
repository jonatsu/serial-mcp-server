"""Parsed script types: steps, the script, and syntax errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from serial_mcp_server.writer import Pacing


class StepKind(StrEnum):
    """What a step does when the runner executes it."""

    BARE = "bare"
    SEND = "send"
    SENDLINE = "sendline"
    SENDHEX = "sendhex"
    EXPECT = "expect"
    PROMPT = "prompt"
    FAIL_ON = "fail_on"
    TIMEOUT = "timeout"
    SLEEP = "sleep"
    DISCARD = "discard"
    LINE = "line"
    PULSE = "pulse"
    PACING = "pacing"
    NEWLINE = "newline"
    MARK = "mark"
    SEND_FILE = "send_file"


@dataclass(frozen=True)
class Step:
    """One bare line or directive, validated and ready to run.

    Attributes:
        line: One-based line number in the script source.
        text: The source line before variable substitution, so results never show variable values.
        kind: What the step does.
        payload: Text to send (BARE, SEND, SENDLINE), the new newline (NEWLINE), the label (MARK),
            the regular expression (EXPECT, PROMPT, FAIL_ON), or the path relative to the files
            directory (SEND_FILE). Variables are already substituted.
        data: Bytes to send (SENDHEX).
        seconds: Timeout (EXPECT with ``--timeout``, TIMEOUT) or wait (SLEEP, PULSE).
        enabled: False for ``@prompt --off`` and ``@fail_on --clear``; the level for LINE.
        line_name: ``dtr`` or ``rts`` (LINE, PULSE).
        pacing: The pacing a PACING step selects; ``Pacing()`` for ``@pacing off``.
        path: The resolved file a SEND_FILE step sends, checked when the script was parsed.
    """

    line: int
    text: str
    kind: StepKind
    payload: str = ""
    data: bytes = b""
    seconds: float | None = None
    enabled: bool = True
    line_name: str = ""
    pacing: Pacing | None = None
    path: Path | None = None


@dataclass(frozen=True)
class Script:
    """A fully validated script."""

    steps: tuple[Step, ...]
    description: str | None  # the first comment line, without its marker
    variable_names: tuple[str, ...]  # names supplied to the parser, never their values
    sha256: str  # of the source text as UTF-8


@dataclass(frozen=True)
class ScriptError:
    """One problem found while parsing."""

    line: int
    text: str
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


class ScriptSyntaxError(ValueError):
    """The script has at least one error; ``errors`` lists all of them, in line order."""

    def __init__(self, errors: tuple[ScriptError, ...]) -> None:
        self.errors = errors
        count = len(errors)
        super().__init__(
            f"{count} error{'s' if count != 1 else ''} in the script: " + "; ".join(map(str, errors))
        )
