"""Tests for serial_mcp_server.config — every SERIAL_MCP_* variable, read once and validated together."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest

import serial_mcp_server
from serial_mcp_server import config
from serial_mcp_server.config import (
    DEFAULT_MIRROR_LINK,
    ConfigError,
    LogFormatName,
    PluginPolicy,
    TextTimestampMode,
    env_name,
)
from serial_mcp_server.portlog_formats import FORMATS, TEXT_TIMESTAMP_MODES

PACKAGE_DIR = Path(serial_mcp_server.__file__).parent
CONFIG_MODULE = PACKAGE_DIR / "config.py"
ENV_READ = re.compile(r"os\.environ|getenv")


def load(**environ: str) -> config.Settings:
    return config.load(environ)


class TestDefaults:
    def test_every_field_has_a_default(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

        settings = config.load({})

        assert settings.data_root == tmp_path / ".serial_mcp"
        assert settings.files_dir == settings.data_root / "files"
        assert settings.scripts_dir == settings.data_root / "scripts"
        assert settings.log_dir == settings.data_root / "logs"
        assert settings.log_enabled is False
        assert settings.log_formats == ("text",)
        assert settings.log_text_timestamps == "off"
        assert settings.log_level == "WARNING"
        assert settings.max_connections == 10
        assert settings.max_write_seconds == 60.0
        assert settings.script_max_seconds == 3600.0
        assert settings.mirror == "off"
        assert settings.mirror_link is None
        assert settings.plugins == PluginPolicy(enabled=False, allowlist=None)
        assert settings.tool_separator == "."
        assert settings.trace_enabled is True
        assert settings.trace_payloads is False
        assert settings.trace_max_bytes == 16384

    def test_a_blank_value_means_the_default(self):
        settings = load(SERIAL_MCP_MAX_CONNECTIONS="  ", SERIAL_MCP_LOG="", SERIAL_MCP_MIRROR=" ")

        assert (settings.max_connections, settings.log_enabled, settings.mirror) == (10, False, "off")

    def test_directories_follow_the_data_root(self, tmp_path):
        settings = load(SERIAL_MCP_SPEC_ROOT=str(tmp_path))

        assert (settings.files_dir, settings.scripts_dir, settings.log_dir) == (
            tmp_path / "files",
            tmp_path / "scripts",
            tmp_path / "logs",
        )

    def test_a_directory_variable_overrides_its_default(self, tmp_path):
        settings = load(SERIAL_MCP_SPEC_ROOT=str(tmp_path), SERIAL_MCP_FILES_DIR=str(tmp_path / "custom"))

        assert settings.files_dir == tmp_path / "custom"
        assert settings.log_dir == tmp_path / "logs"

    def test_variables_without_the_prefix_and_unknown_names_are_ignored(self):
        settings = load(MAX_CONNECTIONS="3", SERIAL_MCP_NOT_A_SETTING="x")

        assert settings.max_connections == 10

    def test_the_model_never_reads_the_process_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SERIAL_MCP_MAX_CONNECTIONS", "3")
        monkeypatch.setenv("MAX_CONNECTIONS", "3")  # the name an unprefixed environment source would read

        assert config.load({}).max_connections == 10
        assert config.Settings(data_root=tmp_path).max_connections == 10  # the constructor's own sources


class TestAcceptedForms:
    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF", "False"])
    def test_trace_off_forms_disable_tracing(self, raw):
        assert load(SERIAL_MCP_TRACE=raw).trace_enabled is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "YES"])
    def test_boolean_on_forms(self, raw):
        assert load(SERIAL_MCP_LOG=raw).log_enabled is True

    def test_choice_values_match_in_any_case(self):
        settings = load(
            SERIAL_MCP_MIRROR="RW", SERIAL_MCP_LOG_TEXT_TIMESTAMPS="System", SERIAL_MCP_LOG_LEVEL="debug"
        )

        assert (settings.mirror, settings.log_text_timestamps, settings.log_level) == (
            "rw",
            "system",
            "DEBUG",
        )

    def test_formats_split_on_commas_and_drop_duplicates(self):
        assert load(SERIAL_MCP_LOG_FORMATS="jsonl, TEXT,jsonl").log_formats == ("jsonl", "text")

    @pytest.mark.skipif(os.name == "nt", reason="the mirror is off on Windows")
    def test_an_enabled_mirror_gets_the_default_link(self):
        assert load(SERIAL_MCP_MIRROR="ro").mirror_link == DEFAULT_MIRROR_LINK

    @pytest.mark.skipif(os.name == "nt", reason="the mirror is off on Windows")
    def test_an_explicit_mirror_link_wins(self, tmp_path):
        settings = load(SERIAL_MCP_MIRROR="rw", SERIAL_MCP_MIRROR_LINK=str(tmp_path / "tty"))

        assert settings.mirror_link == tmp_path / "tty"

    def test_numbers(self):
        settings = load(
            SERIAL_MCP_MAX_CONNECTIONS="3",
            SERIAL_MCP_MAX_WRITE_SECONDS="0.5",
            SERIAL_MCP_SCRIPT_MAX_SECONDS="90",
            SERIAL_MCP_TRACE_MAX_BYTES="100",
        )

        assert (settings.max_connections, settings.max_write_seconds) == (3, 0.5)
        assert (settings.script_max_seconds, settings.trace_max_bytes) == (90.0, 100)

    def test_tool_separator(self):
        assert load(SERIAL_MCP_TOOL_SEPARATOR="_").tool_separator == "_"


class TestRejectedForms:
    @pytest.mark.parametrize(
        ("variable", "raw"),
        [
            ("SERIAL_MCP_TRACE", "maybe"),
            ("SERIAL_MCP_LOG", "2"),
            ("SERIAL_MCP_MIRROR", "bogus"),
            ("SERIAL_MCP_LOG_LEVEL", "loud"),
            ("SERIAL_MCP_LOG_TEXT_TIMESTAMPS", "fast"),
            ("SERIAL_MCP_LOG_FORMATS", "text,xml"),
            ("SERIAL_MCP_LOG_FORMATS", ","),
            ("SERIAL_MCP_MAX_CONNECTIONS", "zero"),
            ("SERIAL_MCP_MAX_CONNECTIONS", "0"),
            ("SERIAL_MCP_MAX_WRITE_SECONDS", "inf"),
            ("SERIAL_MCP_MAX_WRITE_SECONDS", "-1"),
            ("SERIAL_MCP_SCRIPT_MAX_SECONDS", "nan"),
            ("SERIAL_MCP_TRACE_MAX_BYTES", "1.5"),
        ],
    )
    def test_an_invalid_value_is_rejected_by_its_variable_name(self, variable, raw):
        with pytest.raises(ConfigError) as raised:
            config.load({variable: raw})

        assert raised.value.problems
        assert all(line.startswith(f"{variable}=") for line in raised.value.problems)

    def test_a_variable_with_several_problems_gets_one_line(self):
        with pytest.raises(ConfigError) as raised:
            load(SERIAL_MCP_LOG_FORMATS="xml,foo")

        assert len(raised.value.problems) == 1

    def test_critical_is_a_log_level(self):
        assert load(SERIAL_MCP_LOG_LEVEL="critical").log_level == "CRITICAL"

    def test_one_error_names_every_invalid_variable(self):
        with pytest.raises(ConfigError) as raised:
            load(SERIAL_MCP_MIRROR="bogus", SERIAL_MCP_LOG_LEVEL="loud", SERIAL_MCP_MAX_CONNECTIONS="0")

        message = str(raised.value)
        for variable in ("SERIAL_MCP_MIRROR", "SERIAL_MCP_LOG_LEVEL", "SERIAL_MCP_MAX_CONNECTIONS"):
            assert variable in message
        assert len(raised.value.problems) == 3


class TestNames:
    def test_every_field_maps_to_one_variable(self):
        names = [env_name(field) for field in config.Settings.model_fields]

        assert len(set(names)) == len(names)
        assert all(name.startswith("SERIAL_MCP_") for name in names)
        assert env_name("data_root") == "SERIAL_MCP_SPEC_ROOT"

    def test_format_and_timestamp_names_match_the_port_log(self):
        assert set(get_args(LogFormatName)) == set(FORMATS)
        assert set(get_args(TextTimestampMode)) == set(TEXT_TIMESTAMP_MODES)


def test_no_module_but_config_reads_the_environment():
    offenders = [
        f"{path.relative_to(PACKAGE_DIR)}:{number}"
        for path in sorted(PACKAGE_DIR.rglob("*.py"))
        if path != CONFIG_MODULE
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if ENV_READ.search(line)
    ]

    assert offenders == []


def test_the_server_exits_2_and_names_every_invalid_variable(tmp_path):
    env = {
        **os.environ,
        "SERIAL_MCP_SPEC_ROOT": str(tmp_path),
        "SERIAL_MCP_MIRROR": "bogus",
        "SERIAL_MCP_LOG_LEVEL": "loud",
    }

    done = subprocess.run(
        [sys.executable, "-m", "serial_mcp_server"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 2
    assert "SERIAL_MCP_MIRROR" in done.stderr
    assert "SERIAL_MCP_LOG_LEVEL" in done.stderr
    assert "Traceback" not in done.stderr
