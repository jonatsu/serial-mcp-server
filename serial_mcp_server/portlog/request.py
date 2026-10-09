"""Which port log to start: formats, directory, and timestamp mode, from tool arguments and ``Settings``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from serial_mcp_server.portlog.formats import FORMATS, TEXT_TIMESTAMP_MODES, TextTimestamps

if TYPE_CHECKING:
    from serial_mcp_server.config import Settings

DEFAULT_TEXT_TIMESTAMPS = TextTimestamps.OFF


@dataclass(frozen=True)
class LogRequest:
    formats: tuple[str, ...]
    directory: Path
    text_timestamps: TextTimestamps = DEFAULT_TEXT_TIMESTAMPS


def parse_formats(value: Any, origin: str) -> tuple[str, ...]:
    """Validate a list (or comma-separated string) of format names.

    Raises:
        ValueError: *value* is empty, not a list of strings, or names an unknown format.
    """
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not all(isinstance(item, str) for item in items):
        raise ValueError(f"{origin} must be a list of format names.")
    names = tuple(dict.fromkeys(item.strip().lower() for item in items if item.strip()))
    if not names:
        raise ValueError(f"{origin} must name at least one format.")
    unknown = [name for name in names if name not in FORMATS]
    if unknown:
        raise ValueError(f"Unknown log format {unknown[0]!r} in {origin}. Choose from: {', '.join(FORMATS)}.")
    return names


def _parse_text_timestamps(value: Any, settings: Settings) -> TextTimestamps:
    """Validate the timestamp mode from an argument, else take the configured one.

    Raises:
        ValueError: The mode is not one of ``TEXT_TIMESTAMP_MODES``.
    """
    if value is None:
        return TextTimestamps(settings.log_text_timestamps)
    return _text_timestamps(value, "log_text_timestamps")


def _text_timestamps(value: Any, origin: str) -> TextTimestamps:
    mode = value.strip().lower() if isinstance(value, str) else value
    if mode not in TEXT_TIMESTAMP_MODES:
        raise ValueError(f"{origin} must be one of: {', '.join(TEXT_TIMESTAMP_MODES)}.")
    return TextTimestamps(mode)


def resolve_log_request(
    settings: Settings, log: bool | None, formats: Any = None, text_timestamps: Any = None
) -> LogRequest | None:
    """Decide whether a connection logs, and in which formats and directory.

    An argument beats the configuration: ``log=False`` disables logging, ``log=True`` or
    *formats* alone enables it, and otherwise ``settings.log_enabled`` (``SERIAL_MCP_LOG``) decides.

    Raises:
        ValueError: A format list or the timestamp mode is invalid.
    """
    if log is None:
        log = formats is not None or settings.log_enabled
    if not log:
        return None
    return build_log_request(settings, formats, text_timestamps)


def build_log_request(
    settings: Settings, formats: Any = None, text_timestamps: Any = None, formats_origin: str = "log_formats"
) -> LogRequest:
    """Build a request for a log that is definitely wanted, filling gaps from the configuration.

    *formats_origin* names where *formats* came from, so an error points at the caller's own option.

    Raises:
        ValueError: A format list or the timestamp mode is invalid.
    """
    names = parse_formats(formats, formats_origin) if formats is not None else settings.log_formats
    return LogRequest(names, settings.log_dir, _parse_text_timestamps(text_timestamps, settings))
