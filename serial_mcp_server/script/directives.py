"""The directive table: each ``@name`` line's syntax, summary, and parser.

``DIRECTIVES`` is the one list of directives. The parser dispatches on it, the reference in
``docs/scripts.md`` is checked against it, and ``serial.script.template`` returns it.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass

from serial_mcp_server.confined_files import MAX_CONFINED_FILE_BYTES, PathRefused, resolve_confined_file
from serial_mcp_server.port.pacing import MAX_PACING_DELAY_MS, NO_PACING, Pacing
from serial_mcp_server.script.expand import LineContext, LineError, parse_duration
from serial_mcp_server.script.model import Step, StepKind

TIMEOUT_OPTION = "--timeout"
LINE_NAMES = ("dtr", "rts")
_MILLISECOND_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Directive:
    """One directive.

    ``rest_of_line`` directives take everything after the name as one verbatim argument, with
    no quote processing; the others split their arguments with ``shlex`` in POSIX mode.
    """

    name: str
    usage: str
    summary: str
    parse: Callable[[LineContext, list[str]], Step]
    rest_of_line: bool = False


def split_arguments(directive: Directive, remainder: str) -> list[str]:
    """Split the text after a directive name into the arguments its handler expects.

    Raises:
        LineError: A quotation is not closed.
    """
    if directive.rest_of_line:
        return [remainder] if remainder else []
    try:
        return shlex.split(remainder, posix=True)
    except ValueError as exc:
        raise LineError(f"cannot split the arguments: {exc}.") from exc


def _no_arguments(words: list[str], usage: str) -> None:
    if words:
        raise LineError(f"takes no arguments; usage: {usage}")


def _single(words: list[str], usage: str) -> str:
    if len(words) != 1:
        raise LineError(f"expected one argument; usage: {usage}")
    return words[0]


def _step(kind: StepKind, **fields) -> Step:
    """A step without its line and text, which the parser fills in."""
    return Step(line=0, text="", kind=kind, **fields)


def _send(kind: StepKind, usage: str) -> Callable[[LineContext, list[str]], Step]:
    def parse(ctx: LineContext, words: list[str]) -> Step:
        if not words:
            raise LineError(f"expected text to send; usage: {usage}")
        return _step(kind, payload=ctx.escaped(words[0]))

    return parse


def _sendhex(ctx: LineContext, words: list[str]) -> Step:
    if not words:
        raise LineError("expected hex digits; usage: @sendhex HEX")
    digits = "".join(ctx.text(words[0]).split())
    try:
        data = bytes.fromhex(digits)
    except ValueError as exc:
        raise LineError(f"not valid hex: {exc}.") from exc
    if not data:
        raise LineError("expected hex digits; usage: @sendhex HEX")
    return _step(StepKind.SENDHEX, data=data)


def _expect(ctx: LineContext, words: list[str]) -> Step:
    usage = "@expect REGEX [--timeout D]"
    if not words:
        raise LineError(f"expected a pattern; usage: {usage}")
    pattern, options = words[0], words[1:]
    seconds = None
    if options:
        if len(options) != 2 or options[0] != TIMEOUT_OPTION:
            raise LineError(f"unexpected arguments {options}; usage: {usage}")
        seconds = parse_duration(ctx.text(options[1]))
    return _step(StepKind.EXPECT, payload=ctx.pattern(pattern), seconds=seconds)


def _switchable_pattern(
    kind: StepKind, off_option: str, usage: str
) -> Callable[[LineContext, list[str]], Step]:
    def parse(ctx: LineContext, words: list[str]) -> Step:
        word = _single(words, usage)
        if word == off_option:
            return _step(kind, enabled=False)
        return _step(kind, payload=ctx.pattern(word))

    return parse


def _duration(kind: StepKind, usage: str) -> Callable[[LineContext, list[str]], Step]:
    def parse(ctx: LineContext, words: list[str]) -> Step:
        return _step(kind, seconds=parse_duration(ctx.text(_single(words, usage))))

    return parse


def _discard(ctx: LineContext, words: list[str]) -> Step:
    _no_arguments(words, "@discard")
    return _step(StepKind.DISCARD)


def _line(name: str) -> Callable[[LineContext, list[str]], Step]:
    usage = f"@{name} on|off"

    def parse(ctx: LineContext, words: list[str]) -> Step:
        word = ctx.text(_single(words, usage))
        if word not in ("on", "off"):
            raise LineError(f"expected on or off; usage: {usage}")
        return _step(StepKind.LINE, line_name=name, enabled=word == "on")

    return parse


def _pulse(ctx: LineContext, words: list[str]) -> Step:
    usage = "@pulse dtr|rts D"
    if len(words) != 2:
        raise LineError(f"expected a line and a duration; usage: {usage}")
    line_name = ctx.text(words[0])
    if line_name not in LINE_NAMES:
        raise LineError(f"expected a line and a duration; usage: {usage}")
    return _step(StepKind.PULSE, line_name=line_name, seconds=parse_duration(ctx.text(words[1])))


def _delay_ms(word: str) -> int:
    seconds = parse_duration(word)
    milliseconds = seconds * 1000
    if abs(milliseconds - round(milliseconds)) > _MILLISECOND_TOLERANCE:
        raise LineError(f"{word!r} is not a whole number of milliseconds.")
    if not 1 <= round(milliseconds) <= MAX_PACING_DELAY_MS:
        raise LineError(f"a pacing delay must be from 1ms to {MAX_PACING_DELAY_MS}ms.")
    return round(milliseconds)


def _pacing(ctx: LineContext, words: list[str]) -> Step:
    usage = "@pacing off | @pacing char D | @pacing chunk N D"
    words = [ctx.text(word) for word in words]
    if words == ["off"]:
        return _step(StepKind.PACING, pacing=NO_PACING)
    if len(words) == 2 and words[0] == "char":
        return _step(StepKind.PACING, pacing=Pacing(inter_char_delay_ms=_delay_ms(words[1])))
    if len(words) == 3 and words[0] == "chunk":
        chunk_size_error = f"the chunk size must be a positive whole number; usage: {usage}"
        chunk_size_text = words[1]
        if not chunk_size_text.isascii() or not chunk_size_text.isdigit():
            raise LineError(chunk_size_error)
        try:
            chunk_size = int(chunk_size_text)
        except ValueError as exc:
            raise LineError(chunk_size_error) from exc
        if chunk_size < 1:
            raise LineError(chunk_size_error)
        return _step(
            StepKind.PACING, pacing=Pacing(chunk_size=chunk_size, chunk_delay_ms=_delay_ms(words[2]))
        )
    raise LineError(f"usage: {usage}")


def _newline(ctx: LineContext, words: list[str]) -> Step:
    if not words:
        raise LineError("expected the newline text; usage: @newline TEXT")
    newline = ctx.escaped(words[0])
    if not newline:
        raise LineError("the newline cannot be empty.")
    return _step(StepKind.NEWLINE, payload=newline)


def _mark(ctx: LineContext, words: list[str]) -> Step:
    label = ctx.text(words[0]) if words else ""
    if not label:
        raise LineError("expected a label; usage: @mark LABEL")
    return _step(StepKind.MARK, payload=label)


def _send_file(ctx: LineContext, words: list[str]) -> Step:
    relative = ctx.text(_single(words, "@send_file PATH"))
    try:
        resolved = resolve_confined_file(ctx.files_dir, relative, MAX_CONFINED_FILE_BYTES)
    except PathRefused as exc:
        raise LineError(str(exc)) from exc
    return _step(StepKind.SEND_FILE, payload=relative, path=resolved)


def _directive(
    name: str,
    usage: str,
    summary: str,
    parse: Callable[[LineContext, list[str]], Step],
    *,
    rest_of_line: bool = False,
) -> tuple[str, Directive]:
    return name, Directive(name, usage, summary, parse, rest_of_line)


DIRECTIVES: dict[str, Directive] = dict(
    [
        _directive(
            "send",
            "@send TEXT",
            r"Send TEXT with the escapes \r, \n, \t, \xHH, and \\ decoded, and no newline.",
            _send(StepKind.SEND, "@send TEXT"),
            rest_of_line=True,
        ),
        _directive(
            "sendline",
            "@sendline TEXT",
            "As @send, then the newline and the prompt wait. For lines that need escapes or start with # or @.",
            _send(StepKind.SENDLINE, "@sendline TEXT"),
            rest_of_line=True,
        ),
        _directive(
            "sendhex",
            "@sendhex HEX",
            "Send raw bytes given in hex; spaces are allowed.",
            _sendhex,
            rest_of_line=True,
        ),
        _directive(
            "expect",
            "@expect REGEX [--timeout D]",
            "Wait for REGEX in the output after the cursor, then move the cursor past the match.",
            _expect,
        ),
        _directive(
            "prompt",
            "@prompt REGEX | @prompt --off",
            "After each bare line and @sendline, wait for REGEX with the current timeout, consuming it.",
            _switchable_pattern(StepKind.PROMPT, "--off", "@prompt REGEX | @prompt --off"),
        ),
        _directive(
            "fail_on",
            "@fail_on REGEX | @fail_on --clear",
            "From now on, any output matching REGEX fails the run. Several patterns may be active.",
            _switchable_pattern(StepKind.FAIL_ON, "--clear", "@fail_on REGEX | @fail_on --clear"),
        ),
        _directive(
            "timeout",
            "@timeout D",
            "Default timeout for @expect and the prompt wait. Initially 10s.",
            _duration(StepKind.TIMEOUT, "@timeout D"),
        ),
        _directive(
            "sleep",
            "@sleep D",
            "Wait D. Fail patterns stay active.",
            _duration(StepKind.SLEEP, "@sleep D"),
        ),
        _directive(
            "discard",
            "@discard",
            "Drop all output received so far from the matcher window.",
            _discard,
        ),
        _directive("dtr", "@dtr on|off", "Set the DTR control line.", _line("dtr")),
        _directive("rts", "@rts on|off", "Set the RTS control line.", _line("rts")),
        _directive(
            "pulse",
            "@pulse dtr|rts D",
            "Set the line low for D, then high.",
            _pulse,
        ),
        _directive(
            "pacing",
            "@pacing off | @pacing char D | @pacing chunk N D",
            "Pacing for the script's later writes. Without it, the connection's pacing applies.",
            _pacing,
        ),
        _directive(
            "newline",
            "@newline TEXT",
            "Newline for later bare lines, with escapes decoded. Initially the connection's newline.",
            _newline,
            rest_of_line=True,
        ),
        _directive(
            "mark",
            "@mark LABEL",
            "Write a mark event to the port log and record it in the result.",
            _mark,
            rest_of_line=True,
        ),
        _directive(
            "send_file",
            "@send_file PATH",
            "Send a file from the files directory with the current pacing, as serial.send_file does.",
            _send_file,
        ),
    ]
)
