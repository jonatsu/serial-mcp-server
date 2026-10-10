"""Server and command configuration: every ``SERIAL_MCP_*`` variable, read and validated once.

``load`` builds one frozen ``Settings`` from an environment mapping at process start; the server and the
``serial_mcp_script`` command pass it down, and no other module reads the environment. An invalid value stops
the start with a ``ConfigError`` that names every invalid variable at once.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

logger = logging.getLogger(__name__)

ENV_PREFIX = "SERIAL_MCP_"
DATA_DIR_NAME = ".serial_mcp"
DEFAULT_SCRIPT_MAX_SECONDS = 3600.0
DEFAULT_SCRIPT_PATTERN_SECONDS = 2.0
# A documented, overridable default (SERIAL_MCP_MIRROR_LINK), not a temporary file.
DEFAULT_MIRROR_LINK = "/tmp/serial-mcp"  # noqa: S108  # nosec B108

# The port log and the format modules own these names; tests/test_config.py keeps the two lists equal.
LogFormatName = Literal["text", "jsonl", "lines", "raw"]
TextTimestampMode = Literal["off", "elapsed", "system"]
MirrorMode = Literal["off", "ro", "rw"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
# Names the logging module also accepts, which SERIAL_MCP_LOG_LEVEL took before it was validated.
_LOG_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL"}

# Variables whose name is not ENV_PREFIX plus the upper-cased field name.
_ENV_NAMES = {
    "data_root": "SERIAL_MCP_SPEC_ROOT",
    "log_enabled": "SERIAL_MCP_LOG",
    "trace_enabled": "SERIAL_MCP_TRACE",
}
_DIRECTORIES_UNDER_DATA_ROOT = {"files_dir": "files", "scripts_dir": "scripts", "log_dir": "logs"}


class ConfigError(Exception):
    """One or more ``SERIAL_MCP_*`` variables are invalid; ``problems`` holds one line per variable."""

    problems: list[str]

    def __init__(self, problems: list[str]) -> None:
        super().__init__("Invalid configuration:\n" + "\n".join(f"  {line}" for line in problems))
        self.problems = problems


@dataclass(frozen=True)
class PluginPolicy:
    """Which plugins may load: none, all (``allowlist`` is ``None``), or only the named ones."""

    enabled: bool = False
    allowlist: frozenset[str] | None = None

    @classmethod
    def parse(cls, raw: str) -> PluginPolicy:
        """Parse ``SERIAL_MCP_PLUGINS``: empty for none, ``*`` or ``all`` for all, else a comma-separated list."""
        text = raw.strip()
        if not text:
            return cls()
        if text in ("*", "all"):
            return cls(enabled=True)
        names = frozenset(name.strip() for name in text.split(",") if name.strip())
        return cls(enabled=True, allowlist=names) if names else cls()


def find_data_root(start: Path) -> Path:
    """The ``.serial_mcp`` directory for a working directory *start*.

    The first existing ``.serial_mcp`` from *start* upwards, else one next to the nearest ``.git``, else one in
    *start*.
    """
    ancestors = [start, *start.parents]
    for directory in ancestors:
        if (directory / DATA_DIR_NAME).is_dir():
            return directory / DATA_DIR_NAME
    for directory in ancestors:
        if (directory / ".git").is_dir():
            return directory / DATA_DIR_NAME
    return start / DATA_DIR_NAME


class Settings(BaseSettings):
    """Every configurable value of the server and the script command, with its default.

    Build it with ``load``, which maps the environment onto the fields; ``load({})`` gives the defaults.
    Directory fields left unset default to a directory under ``data_root``.
    """

    model_config = SettingsConfigDict(frozen=True, extra="forbid")

    data_root: Path
    files_dir: Path
    scripts_dir: Path
    log_dir: Path
    log_enabled: bool = False
    log_formats: Annotated[tuple[LogFormatName, ...], NoDecode] = Field(default=("text",), min_length=1)
    log_text_timestamps: TextTimestampMode = "off"
    log_level: LogLevel = "WARNING"
    # Open connections in this process; one more fails the open with ``limit_reached``.
    max_connections: int = Field(default=10, gt=0)
    # Cap on the estimated time of one paced write or file send, checked before any byte is sent. An unpaced
    # ``serial.write`` has no estimate and is not capped. See ``mcp.writes.duration_error``.
    max_write_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    # Wall-clock limit of one script run; a run that reaches it ends ``failed`` with reason ``max_duration``.
    script_max_seconds: float = Field(default=DEFAULT_SCRIPT_MAX_SECONDS, gt=0, allow_inf_nan=False)
    # Limit of one regular-expression search in a script; a pattern that runs longer ends the run ``errored``
    # with reason ``regex_timeout``.
    script_pattern_seconds: float = Field(default=DEFAULT_SCRIPT_PATTERN_SECONDS, gt=0, allow_inf_nan=False)
    mirror: MirrorMode = "off"
    # A prefix, not a path: the mirror appends a number, so a trailing slash puts the links in a directory.
    mirror_link: str | None = None
    plugins: PluginPolicy = PluginPolicy()
    tool_separator: str = Field(default=".", min_length=1)
    trace_enabled: bool = True
    trace_payloads: bool = False
    # Characters, not bytes, kept of each traced payload; see ``mcp.trace.TraceBuffer``.
    trace_max_bytes: int = Field(default=16384, gt=0)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read only the values passed in, so the model never reads the process environment by itself."""
        return (init_settings,)

    @model_validator(mode="before")
    @classmethod
    def _fill_derived_defaults(cls, values: Any) -> Any:
        """Default the directories under ``data_root``, and settle the mirror for this platform."""
        if not isinstance(values, dict):
            return values
        filled = dict(values)
        data_root = Path(filled["data_root"]) if "data_root" in filled else find_data_root(Path.cwd())
        filled["data_root"] = data_root
        for field_name, directory_name in _DIRECTORIES_UNDER_DATA_ROOT.items():
            filled.setdefault(field_name, data_root / directory_name)
        mirror = filled.get("mirror", "off")
        mirror_on = isinstance(mirror, str) and mirror.strip().lower() in ("ro", "rw")
        if mirror_on and os.name == "nt":
            logger.warning("PTY mirror is not supported on Windows. Ignoring SERIAL_MCP_MIRROR=%r.", mirror)
            filled["mirror"] = "off"
        elif mirror_on:
            filled.setdefault("mirror_link", DEFAULT_MIRROR_LINK)
        return filled

    @field_validator("log_formats", mode="before")
    @classmethod
    def _split_formats(cls, value: Any) -> Any:
        items = value.split(",") if isinstance(value, str) else value
        if isinstance(items, (list, tuple)):
            return tuple(dict.fromkeys(str(item).strip().lower() for item in items if str(item).strip()))
        return value

    @field_validator("log_text_timestamps", "mirror", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        level = value.strip().upper()
        return _LOG_LEVEL_ALIASES.get(level, level)

    @field_validator("plugins", mode="before")
    @classmethod
    def _parse_plugins(cls, value: Any) -> Any:
        return PluginPolicy.parse(value) if isinstance(value, str) else value


def env_name(field_name: str) -> str:
    """The environment variable that sets *field_name*."""
    return _ENV_NAMES.get(field_name, ENV_PREFIX + field_name.upper())


_FIELD_FOR_ENV = {env_name(name): name for name in Settings.model_fields}


def load(environ: Mapping[str, str] = os.environ) -> Settings:
    """Build ``Settings`` from the ``SERIAL_MCP_*`` entries of *environ*; a blank value means the default.

    Raises:
        ConfigError: One or more values are invalid; the message names each variable.
    """
    values: dict[str, str] = {}
    for name, raw in environ.items():
        field_name = _FIELD_FOR_ENV.get(name)
        if field_name is not None and raw.strip():
            values[field_name] = raw.strip()
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        raise ConfigError(_problems(exc, values)) from None


def _problems(error: ValidationError, values: Mapping[str, str]) -> list[str]:
    """One line per invalid variable, with the first reason found for it."""
    problems: dict[str, str] = {}
    for detail in error.errors(include_url=False):
        field_name = str(detail["loc"][0]) if detail["loc"] else ""
        variable = env_name(field_name) if field_name in Settings.model_fields else field_name
        problems.setdefault(variable, f"{variable}={values.get(field_name, '')!r}: {detail['msg']}")
    return list(problems.values())
