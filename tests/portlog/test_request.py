"""Tests for serial_mcp_server.portlog.request — choosing formats, directory, and timestamps."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from serial_mcp_server import config
from serial_mcp_server.config import ConfigError, Settings
from serial_mcp_server.portlog.request import build_log_request, resolve_log_request

WAIT_S = 2.0
NOW = datetime(2026, 10, 8, 14, 5, 30)  # naive local time, as the file name uses
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="needs POSIX file modes and symlinks")


def configured(tmp_path, **environ: str) -> Settings:
    """Settings as the server loads them from *environ*, with the data root under tmp_path/spec."""
    return config.load({"SERIAL_MCP_SPEC_ROOT": str(tmp_path / "spec"), **environ})


class TestResolveLogRequest:
    def test_off_by_default(self, tmp_path):
        assert resolve_log_request(configured(tmp_path), None) is None

    @pytest.mark.parametrize("value", ["1", "true", "YES"])
    def test_environment_enables_logging_in_text_format(self, tmp_path, value):
        request = resolve_log_request(configured(tmp_path, SERIAL_MCP_LOG=value), None)

        assert request is not None
        assert request.formats == ("text",)

    def test_environment_formats_apply_when_logging_is_enabled(self, tmp_path):
        settings = configured(tmp_path, SERIAL_MCP_LOG="1", SERIAL_MCP_LOG_FORMATS="text, jsonl")
        assert resolve_log_request(settings, None).formats == ("text", "jsonl")

    def test_environment_formats_alone_do_not_enable_logging(self, tmp_path):
        assert resolve_log_request(configured(tmp_path, SERIAL_MCP_LOG_FORMATS="jsonl"), None) is None

    def test_false_overrides_the_environment(self, tmp_path):
        assert resolve_log_request(configured(tmp_path, SERIAL_MCP_LOG="1"), False) is None

    def test_false_overrides_formats(self, tmp_path):
        assert resolve_log_request(configured(tmp_path), False, ["jsonl"]) is None

    def test_true_works_with_the_environment_unset(self, tmp_path):
        assert resolve_log_request(configured(tmp_path), True).formats == ("text",)

    def test_formats_alone_enable_logging(self, tmp_path):
        assert resolve_log_request(configured(tmp_path), None, ["jsonl"]).formats == ("jsonl",)

    def test_argument_formats_override_environment_formats(self, tmp_path):
        settings = configured(tmp_path, SERIAL_MCP_LOG_FORMATS="jsonl")
        assert resolve_log_request(settings, True, ["text"]).formats == ("text",)

    def test_formats_accept_a_comma_separated_string_and_drop_duplicates(self, tmp_path):
        assert resolve_log_request(configured(tmp_path), True, "jsonl, text,jsonl").formats == (
            "jsonl",
            "text",
        )

    @pytest.mark.parametrize("bad", [["nope"], [], "", [1], 5, ["text", "xml"]])
    def test_invalid_formats_are_rejected(self, tmp_path, bad):
        with pytest.raises(ValueError, match="log_formats"):
            resolve_log_request(configured(tmp_path), True, bad)

    def test_invalid_environment_formats_name_the_variable(self, tmp_path):
        with pytest.raises(ConfigError, match="SERIAL_MCP_LOG_FORMATS"):
            configured(tmp_path, SERIAL_MCP_LOG="1", SERIAL_MCP_LOG_FORMATS="nope")

    def test_directory_defaults_to_logs_under_the_spec_root(self, tmp_path):
        assert resolve_log_request(configured(tmp_path), True).directory == tmp_path / "spec" / "logs"

    def test_directory_comes_from_the_environment(self, tmp_path):
        settings = configured(tmp_path, SERIAL_MCP_LOG_DIR=str(tmp_path / "elsewhere"))
        assert resolve_log_request(settings, True).directory == tmp_path / "elsewhere"


class TestBuildLogRequestOrigin:
    def test_an_unknown_format_names_log_formats_by_default(self, tmp_path):
        with pytest.raises(ValueError, match="'bogus' in log_formats"):
            build_log_request(configured(tmp_path), ["bogus"])

    def test_a_caller_can_name_its_own_option_in_the_error(self, tmp_path):
        with pytest.raises(ValueError, match="'bogus' in --log-format") as raised:
            build_log_request(configured(tmp_path), ["bogus"], formats_origin="--log-format")

        assert "log_formats" not in str(raised.value)
