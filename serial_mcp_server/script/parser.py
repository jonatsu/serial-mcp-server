"""Turn script text and variables into a validated ``Script``, or a complete list of errors.

Parsing is pure apart from checking ``@send_file`` paths on disk, and it never opens a port.
Nothing is sent to a device until the whole script has parsed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from serial_mcp_server.script.directives import DIRECTIVES, split_arguments
from serial_mcp_server.script.expand import VARIABLE_NAME, LineContext, LineError
from serial_mcp_server.script.model import Script, ScriptError, ScriptSyntaxError, Step, StepKind

COMMENT_MARKER = "#"
DIRECTIVE_MARKER = "@"
ESCAPED_DIRECTIVE_MARKER = "@@"
_LINE_END = re.compile(r"\r?\n")
_DIRECTIVE_LINE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)(?:\s+(.*))?\s*", re.DOTALL)


def parse(source: str, variables: Mapping[str, str], files_dir: Path) -> Script:
    """Parse *source* into a ``Script``.

    A line is blank, a comment (``#`` after optional whitespace), a bare line that is sent as
    written, ``@@text`` which sends ``@text``, or ``@name args`` for a directive in
    ``DIRECTIVES``. ``{{NAME}}`` is replaced from *variables*.

    Args:
        source: The script text.
        variables: Variable names and values. A value stays one argument, whatever it contains.
        files_dir: The directory ``@send_file`` paths must resolve inside.

    Raises:
        ValueError: A variable name is invalid.
        ScriptSyntaxError: The script has errors; ``errors`` lists every one, with its line.
    """
    for name in variables:
        if not VARIABLE_NAME.fullmatch(name):
            raise ValueError(
                f"Invalid variable name {name!r}: use letters, digits, and underscores, "
                "and start with a letter or underscore."
            )
    context = LineContext(variables, files_dir)
    steps: list[Step] = []
    errors: list[ScriptError] = []
    description: str | None = None

    for number, text in enumerate(_lines(source), start=1):
        if description is None and _is_comment(text):
            description = text.strip()[len(COMMENT_MARKER) :].strip() or None
        try:
            step = _parse_line(text, context)
        except LineError as exc:
            errors.append(ScriptError(number, text, str(exc)))
            continue
        if step is not None:
            steps.append(replace(step, line=number, text=text))

    if errors:
        raise ScriptSyntaxError(tuple(errors))
    return Script(
        steps=tuple(steps),
        description=description,
        variable_names=tuple(sorted(variables)),
        sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )


def _lines(source: str) -> list[str]:
    lines = _LINE_END.split(source)
    if lines and lines[-1] == "":  # the file ended with a line terminator
        lines.pop()
    return lines


def _is_comment(text: str) -> bool:
    return text.lstrip().startswith(COMMENT_MARKER)


def _parse_line(text: str, context: LineContext) -> Step | None:
    if not text.strip() or _is_comment(text):
        return None
    if text.startswith(ESCAPED_DIRECTIVE_MARKER):
        return Step(line=0, text="", kind=StepKind.BARE, payload=context.text(text[1:]))
    if text.startswith(DIRECTIVE_MARKER):
        return _parse_directive(text, context)
    return Step(line=0, text="", kind=StepKind.BARE, payload=context.text(text))


def _parse_directive(text: str, context: LineContext) -> Step:
    match = _DIRECTIVE_LINE.fullmatch(text)
    if match is None:
        raise LineError("a directive is @ followed by its name; write @@ to send a line that starts with @.")
    name, remainder = match.group(1), (match.group(2) or "").strip()
    directive = DIRECTIVES.get(name)
    if directive is None:
        raise LineError(f"unknown directive @{name}; known directives: {', '.join(sorted(DIRECTIVES))}.")
    return directive.parse(context, split_arguments(directive, remainder))
