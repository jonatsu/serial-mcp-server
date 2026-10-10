"""Private file creation: owner-only files with collision-free names that never follow a symlink.

The port log and the script runner write files that can hold console output, including passwords, so both
create them through these helpers.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

FILE_MODE = 0o600
DIR_MODE = 0o700
# Numbers tried for one base name before giving up. Names carry the minute and no seconds, so a minute that
# already holds 1000 files of one kind (port logs sharing a format, or script results) makes the next create
# fail with OSError.
MAX_NAME_COLLISIONS = 1000


def log_base_name(prefix: str, now: datetime, number: int) -> str:
    """``<prefix>-<DDMMYYYY>-<HHMM>`` with ``-<number>`` appended after the first."""
    base = f"{prefix}-{now:%d%m%Y-%H%M}"
    return base if number == 0 else f"{base}-{number}"


def open_exclusive(path: Path) -> BinaryIO:
    """Create *path* for writing; never follow a symlink or reuse an existing file.

    Raises:
        FileExistsError: Something already has this name, including a symlink.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, FILE_MODE), "wb")
