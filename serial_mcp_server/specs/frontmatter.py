"""Spec front matter: parse the YAML header, validate its fields, and derive a stable spec id."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Frontmatter regex: matches `---\n<yaml>\n---\n` at the start of content
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?\n)---\s*\n",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML front-matter from markdown content.

    Returns ``(meta_dict, body)`` where *body* is everything after the
    closing ``---``.  If no front-matter is found, returns ``({}, content)``.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content

    yaml_text = match.group(1)
    body = content[match.end() :]

    try:
        meta = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return {}, content

    if not isinstance(meta, dict):
        return {}, content

    return meta, body


def validate_spec_meta(meta: dict[str, Any]) -> list[str]:
    """Validate spec metadata. Returns a list of error strings (empty = valid).

    Required fields:
    - ``kind`` must be ``"serial-protocol"``
    - ``name`` must be a non-empty string
    """
    errors: list[str] = []
    if meta.get("kind") != "serial-protocol":
        errors.append("Missing or invalid 'kind': must be 'serial-protocol'")
    if not meta.get("name") or not isinstance(meta.get("name"), str):
        errors.append("Missing or invalid 'name': must be a non-empty string")
    return errors


# ---------------------------------------------------------------------------
# Spec ID
# ---------------------------------------------------------------------------


def compute_spec_id(path: Path) -> str:
    """Compute a stable spec ID from a file path.

    Uses SHA-256 of the normalized absolute path, truncated to 16 hex chars.
    """
    normalized = str(path.resolve())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
