"""Helpers shared by test modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSON Lines file into a list of objects."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
