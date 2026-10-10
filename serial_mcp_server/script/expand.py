"""Line context, variable substitution, escape decoding, and durations for the script parser."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import regex

VARIABLE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m)")
_DURATION_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0}
_SIMPLE_ESCAPES = {"r": "\r", "n": "\n", "t": "\t", "\\": "\\"}
_HEX_DIGITS = 2


class LineError(Exception):
    """A problem with one script line; the parser attaches the line number and text."""


@dataclass(frozen=True)
class LineContext:
    """What a directive handler needs: the variables and the files directory."""

    variables: Mapping[str, str]
    files_dir: Path

    def text(self, template: str) -> str:
        """Substitute ``{{NAME}}`` variables; values are inserted verbatim."""
        return expand(template, self.variables)

    def escaped(self, template: str) -> str:
        """Decode ``\\r \\n \\t \\xHH \\\\`` in the template, then substitute variables verbatim.

        Decoding first means a backslash inside a variable value stays a backslash.
        """
        return expand(template, self.variables, decode_escapes=True)

    def pattern(self, template: str) -> str:
        """Substitute variables with ``regex.escape`` so their values match literally, and check the pattern.

        The check compiles with ``regex``, the engine the runner searches with, so both accept the same syntax.
        """
        pattern = expand(template, self.variables, for_pattern=True)
        if not pattern:
            raise LineError("the pattern is empty.")
        try:
            _ = regex.compile(pattern)
        except regex.error as exc:
            raise LineError(f"invalid regular expression: {exc}.") from exc
        return pattern


def expand(
    template: str, variables: Mapping[str, str], *, decode_escapes: bool = False, for_pattern: bool = False
) -> str:
    """Replace ``{{NAME}}`` with its value, optionally decoding escapes in the literal text around it.

    Raises:
        LineError: A name has no value, or an escape is malformed.
    """
    parts: list[str] = []
    position = 0
    for match in VARIABLE.finditer(template):
        parts.append(_literal(template[position : match.start()], decode_escapes))
        name = match.group(1)
        if name not in variables:
            raise LineError(f"undefined variable {{{{{name}}}}}.")
        parts.append(regex.escape(variables[name]) if for_pattern else variables[name])
        position = match.end()
    parts.append(_literal(template[position:], decode_escapes))
    return "".join(parts)


def _literal(text: str, decode_escapes: bool) -> str:
    return decode_escape_sequences(text) if decode_escapes else text


def decode_escape_sequences(text: str) -> str:
    """Decode ``\\r``, ``\\n``, ``\\t``, ``\\xHH``, and ``\\\\``; any other backslash sequence is an error.

    ``\\xHH`` gives the character U+00HH, not a byte. The runner encodes the text with the connection's
    encoding, so under UTF-8 a value above ``\\x7f`` goes out as two bytes. Use ``@sendhex`` for raw bytes.
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        escape = text[index + 1 : index + 2]
        if escape in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[escape])
            index += 2
        elif escape == "x":
            digits = text[index + 2 : index + 2 + _HEX_DIGITS]
            if len(digits) != _HEX_DIGITS or not all(c in "0123456789abcdefABCDEF" for c in digits):
                raise LineError("\\x must be followed by two hex digits.")
            out.append(chr(int(digits, 16)))
            index += 2 + _HEX_DIGITS
        elif escape == "":
            raise LineError("a backslash cannot end the text; write \\\\ for one backslash.")
        else:
            raise LineError(f"unknown escape \\{escape}; use \\r, \\n, \\t, \\xHH, or \\\\.")
    return "".join(out)


def parse_duration(word: str) -> float:
    """Parse ``250ms``, ``10s``, ``1.5m``, and the like into seconds.

    Raises:
        LineError: The word has no unit or is not a number with one.
    """
    match = DURATION.fullmatch(word)
    if match is None:
        if re.fullmatch(r"\d+(?:\.\d+)?", word):
            raise LineError(f"duration {word!r} needs a unit: ms, s, or m.")
        raise LineError(f"{word!r} is not a duration; write a number with a unit, such as 500ms, 10s, or 2m.")
    return float(match.group(1)) * _DURATION_SECONDS[match.group(2)]
