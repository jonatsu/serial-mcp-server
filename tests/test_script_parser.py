"""Behavioral tests for parsing script source into validated steps."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from serial_mcp_server.port.pacing import NO_PACING, Pacing
from serial_mcp_server.script import DIRECTIVES, ScriptSyntaxError, StepKind, parse


@pytest.fixture()
def files_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.parametrize(
    ("source", "kind", "expected_fields", "matching_output"),
    [
        ('@send "quoted text"', StepKind.SEND, {"payload": '"quoted text"'}, None),
        ("@sendline # text", StepKind.SENDLINE, {"payload": "# text"}, None),
        ("@sendhex 00 ff", StepKind.SENDHEX, {"data": b"\x00\xff"}, None),
        (
            '@expect "Bytes transferred" --timeout 500ms',
            StepKind.EXPECT,
            {"seconds": 0.5},
            "Bytes transferred",
        ),
        ('@prompt "=> "', StepKind.PROMPT, {}, "=> "),
        ("@prompt --off", StepKind.PROMPT, {"enabled": False}, None),
        ("@fail_on panic", StepKind.FAIL_ON, {"payload": "panic"}, None),
        ("@fail_on --clear", StepKind.FAIL_ON, {"enabled": False}, None),
        ("@timeout 10s", StepKind.TIMEOUT, {"seconds": 10.0}, None),
        ("@sleep 250ms", StepKind.SLEEP, {"seconds": 0.25}, None),
        ("@discard", StepKind.DISCARD, {}, None),
        ("@dtr on", StepKind.LINE, {"line_name": "dtr", "enabled": True}, None),
        ("@rts off", StepKind.LINE, {"line_name": "rts", "enabled": False}, None),
        ("@pulse dtr 1.5m", StepKind.PULSE, {"line_name": "dtr", "seconds": 90.0}, None),
        ("@pacing off", StepKind.PACING, {"pacing": NO_PACING}, None),
        ("@pacing char 1ms", StepKind.PACING, {"pacing": Pacing(inter_char_delay_ms=1)}, None),
        (
            "@pacing chunk 2 1ms",
            StepKind.PACING,
            {"pacing": Pacing(chunk_size=2, chunk_delay_ms=1)},
            None,
        ),
        (r"@newline \r\n", StepKind.NEWLINE, {"payload": "\r\n"}, None),
        ("@mark boot stage", StepKind.MARK, {"payload": "boot stage"}, None),
        ("@send_file image.bin", StepKind.SEND_FILE, {"payload": "image.bin"}, None),
    ],
)
def test_directive_forms_parse_to_their_steps(
    files_dir: Path,
    source: str,
    kind: StepKind,
    expected_fields: dict[str, object],
    matching_output: str | None,
):
    (files_dir / "image.bin").write_bytes(b"firmware")

    step = parse(source, {}, files_dir).steps[0]

    assert step.kind == kind
    for field, expected in expected_fields.items():
        assert getattr(step, field) == expected
    if matching_output is not None:
        assert re.fullmatch(step.payload, matching_output) is not None


def test_bare_comments_and_escaped_directives_keep_their_line_content(files_dir: Path):
    script = parse(" \n \t# comment\nversion\n  command\n@@x\n  @send later\n${loadaddr}\n", {}, files_dir)

    assert [(step.line, step.payload) for step in script.steps] == [
        (3, "version"),
        (4, "  command"),
        (5, "@x"),
        (6, "  @send later"),
        (7, "${loadaddr}"),
    ]


def test_variable_values_are_substituted_after_argument_splitting(files_dir: Path):
    script = parse("@expect {{EXPECTED}}", {"EXPECTED": "boot ready v1.2"}, files_dir)
    pattern = script.steps[0].payload

    assert re.fullmatch(pattern, "boot ready v1.2")
    assert not re.fullmatch(pattern, "boot ready v1X2")


def test_variable_substitution_accepts_a_pulse_line_name(files_dir: Path):
    step = parse("@pulse {{LINE}} 1s", {"LINE": "dtr"}, files_dir).steps[0]

    assert (step.line_name, step.seconds) == ("dtr", 1.0)


def test_expect_timeout_accepts_a_variable_duration(files_dir: Path):
    step = parse("@expect ready --timeout {{TIMEOUT}}", {"TIMEOUT": "500ms"}, files_dir).steps[0]

    assert step.seconds == 0.5


def test_escape_decoding_does_not_change_backslashes_in_variable_values(files_dir: Path):
    script = parse(r"@send prefix\n{{VALUE}}", {"VALUE": r"\n"}, files_dir)

    assert script.steps[0].payload == "prefix\n\\n"


def test_step_text_retains_the_unexpanded_source(files_dir: Path):
    source = "setenv serverip {{SERVER_IP}}"

    step = parse(source, {"SERVER_IP": "192.0.2.1"}, files_dir).steps[0]

    assert step.payload == "setenv serverip 192.0.2.1"
    assert step.text == source


def test_undefined_variables_report_their_source_line(files_dir: Path):
    with pytest.raises(ScriptSyntaxError) as caught:
        parse("version\n@expect {{MISSING}}", {}, files_dir)

    assert [(error.line, error.text) for error in caught.value.errors] == [(2, "@expect {{MISSING}}")]


def test_parser_reports_all_syntax_errors_in_source_order(files_dir: Path):
    source = '@unknown\n@timeout 10\n@expect "unclosed\n@pacing chunk ² 1ms'

    with pytest.raises(ScriptSyntaxError) as caught:
        parse(source, {}, files_dir)

    assert [error.line for error in caught.value.errors] == [1, 2, 3, 4]
    assert [error.text for error in caught.value.errors] == source.splitlines()


def test_send_file_outside_the_files_directory_is_rejected_even_when_the_file_exists(tmp_path: Path):
    files = tmp_path / "files"
    files.mkdir()
    (tmp_path / "outside.bin").write_bytes(b"secret")

    with pytest.raises(ScriptSyntaxError) as caught:
        parse("version\n@send_file ../outside.bin", {}, files)

    assert [error.line for error in caught.value.errors] == [2]
    assert "must name a file inside the files directory" in caught.value.errors[0].message


def test_send_file_step_keeps_the_resolved_confined_path(files_dir: Path):
    path = files_dir / "image.bin"
    path.write_bytes(b"firmware")

    step = parse("@send_file image.bin", {}, files_dir).steps[0]

    assert step.path == path.resolve()


def test_invalid_variable_names_are_rejected_with_the_rule(files_dir: Path):
    with pytest.raises(ValueError, match="Invalid variable name") as raised:
        parse("", {"BAD-NAME": "value"}, files_dir)

    assert "start with a letter or underscore" in str(raised.value)


def test_a_variable_name_may_not_start_with_a_digit(files_dir: Path):
    with pytest.raises(ValueError, match="Invalid variable name '1BAD'"):
        parse("", {"1BAD": "value"}, files_dir)


def test_hex_escape_in_text_decodes_to_the_code_point(files_dir: Path):
    assert parse(r"@send A\x41\x7f", {}, files_dir).steps[0].payload == "AA\x7f"


def test_tab_escape_in_text_decodes_to_a_tab(files_dir: Path):
    assert parse(r"@send a\tb", {}, files_dir).steps[0].payload == "a\tb"


def test_unknown_escape_is_a_parse_error_naming_the_escape(files_dir: Path):
    with pytest.raises(ScriptSyntaxError) as caught:
        parse(r"@send a\qb", {}, files_dir)

    assert [(error.line, "unknown escape \\q" in error.message) for error in caught.value.errors] == [
        (1, True)
    ]


def test_trailing_backslash_is_a_parse_error(files_dir: Path):
    with pytest.raises(ScriptSyntaxError) as caught:
        parse("@send abc\\", {}, files_dir)

    assert "a backslash cannot end the text" in caught.value.errors[0].message


def test_crlf_line_ends_are_removed_and_numbered_like_lf(files_dir: Path):
    script = parse("@send one\r\n@send two\r\n", {}, files_dir)

    assert [(step.line, step.payload, step.text) for step in script.steps] == [
        (1, "one", "@send one"),
        (2, "two", "@send two"),
    ]


def test_description_is_the_first_comment_without_its_marker(files_dir: Path):
    script = parse("@send x\n#   U-Boot smoke test  \n# second comment\n", {}, files_dir)

    assert script.description == "U-Boot smoke test"


def test_documented_directive_names_match_the_parser_table():
    documentation = Path(__file__).parents[1] / "docs" / "scripts.md"
    directive_table = documentation.read_text(encoding="utf-8").split("## Directives\n", 1)[1]
    directive_table = directive_table.split("\n## ", 1)[0]
    documented = re.findall(r"^\| `@([a-z_]+)` \|", directive_table, re.MULTILINE)

    assert documented == list(DIRECTIVES)


def test_importing_the_script_package_does_not_load_mcp():
    check_import = (
        "import sys; import serial_mcp_server.script; "
        "assert 'mcp' not in sys.modules, 'script package imported mcp'"
    )
    # The executable and source are constants; this test needs an isolated interpreter.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", check_import],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
