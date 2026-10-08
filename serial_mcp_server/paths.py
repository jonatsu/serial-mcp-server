"""Confined file access: read a file only if it lies strictly inside an operator-chosen directory.

Imports nothing from ``mcp``, so callers outside the MCP layer, such as the script engine, can reuse it.
An agent supplies the relative path; the operator chooses the directory. Without the confinement an
agent could send any readable file, such as an SSH key, out over a serial line.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePath, PureWindowsPath

from serial_mcp_server.specs import resolve_spec_root

ENV_FILES_DIR = "SERIAL_MCP_FILES_DIR"
FILES_DIR_NAME = "files"
READ_CHUNK_BYTES = 64 * 1024
MAX_CONFINED_FILE_BYTES = 1_048_576  # 1 MiB; a confined file is held in memory whole

# O_BINARY keeps Windows from translating line endings; the others are absent on some platforms.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


class PathRefused(ValueError):
    """The requested path is outside the directory, missing, not a regular file, or too large."""


def files_dir() -> Path:
    """The directory agent-named files may be read from: ``SERIAL_MCP_FILES_DIR``, else ``<spec root>/files``.

    Read on each call, so a change to the environment applies to the next request.
    """
    configured = os.environ.get(ENV_FILES_DIR, "").strip()
    return Path(configured) if configured else resolve_spec_root() / FILES_DIR_NAME


def resolve_confined_file(root: Path, relative: str, max_bytes: int) -> Path:
    """Return the resolved path of a regular file strictly inside *root*.

    The path must be relative. Symbolic links are followed before the containment check, so a
    link that points outside *root* is refused. The directory itself is refused as well. The
    messages name the requested path, never the place a link led to.

    Raises:
        PathRefused: *relative* is empty or absolute, escapes *root*, does not exist, is not a
            regular file (a directory, FIFO, or device), or is larger than *max_bytes*.
    """
    return _resolve_checked(root, relative, max_bytes)[0]


def read_confined_file(root: Path, relative: str, max_bytes: int) -> tuple[Path, bytes]:
    """Read a confined regular file, confirming on the open handle that it is the file that was checked.

    Between the path check and the open, someone with write access to the directory could swap a
    file or a parent directory. Three checks on the opened file close that window: ``fstat`` must
    show the same device and inode as the path check and a regular file within the size limit;
    where the platform can name the open file, its real path must still lie inside *root*; and
    ``O_NONBLOCK`` and ``O_NOFOLLOW`` stop a FIFO from hanging the open and a link from replacing
    the final component.

    Raises:
        PathRefused: As ``resolve_confined_file``, or the file changed after the first check.
        OSError: The file could not be opened or read.
    """
    resolved, checked, root_resolved = _resolve_checked(root, relative, max_bytes)
    fd = os.open(resolved, _OPEN_FLAGS)
    try:
        opened = os.fstat(fd)
        _check_regular_and_size(opened, relative, max_bytes)
        if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
            raise PathRefused(f"path {relative!r} changed while it was being read.")
        _check_still_inside(fd, root_resolved, relative)
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, READ_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:  # the file grew after the size check
                raise PathRefused(f"file {relative!r} is larger than the {max_bytes}-byte limit.")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return resolved, b"".join(chunks)


def _is_absolute(relative: str) -> bool:
    """True for a path anchored at a root or drive on any platform, including UNC paths."""
    windows = PureWindowsPath(relative)
    return PurePath(relative).is_absolute() or bool(windows.drive) or bool(windows.root)


def _resolve_checked(root: Path, relative: str, max_bytes: int) -> tuple[Path, os.stat_result, Path]:
    """Resolve *relative* under *root*; return the path, the ``stat`` taken for it, and the resolved root."""
    if not isinstance(relative, str) or not relative.strip():
        raise PathRefused("path must be a non-empty string.")
    # Refuse before resolving anything: resolving a UNC path on Windows would touch the network.
    if _is_absolute(relative):
        raise PathRefused(f"path {relative!r} must be relative to the files directory.")
    root_resolved = root.resolve()
    try:
        candidate = (root_resolved / relative).resolve()
    except (OSError, ValueError, RuntimeError) as exc:  # NUL byte, symbolic-link loop, bad component
        raise PathRefused(f"path {relative!r} cannot be resolved.") from exc
    if candidate == root_resolved or root_resolved not in candidate.parents:
        raise PathRefused(f"path {relative!r} must name a file inside the files directory.")
    try:
        info = candidate.stat()
    except OSError as exc:
        raise PathRefused(f"no file at path {relative!r} in the files directory.") from exc
    _check_regular_and_size(info, relative, max_bytes)
    return candidate, info, root_resolved


def _check_regular_and_size(info: os.stat_result, relative: str, max_bytes: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise PathRefused(f"path {relative!r} is not a regular file.")
    if info.st_size > max_bytes:
        raise PathRefused(f"file {relative!r} is {info.st_size} bytes; the limit is {max_bytes}.")


def _check_still_inside(fd: int, root_resolved: Path, relative: str) -> None:
    """Where the platform names an open file's path (Linux), require it to lie inside the root."""
    try:
        actual = Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return  # no /proc on this platform; the identity check above still applies
    if root_resolved not in actual.parents:
        raise PathRefused(f"path {relative!r} must name a file inside the files directory.")
