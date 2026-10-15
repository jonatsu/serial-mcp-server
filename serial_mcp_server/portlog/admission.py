"""Bounded, immutable JSON snapshots of metadata retained by port-log admission."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from serial_mcp_server.portlog.formats import Entry

# Reject a whole metadata snapshot beyond these limits. They bound both retained metadata and admission
# work, rather than Python heap usage; raw payload bytes are charged separately by PortLog.
METADATA_LIMIT_BYTES = 64 * 1024
METADATA_MAX_DEPTH = 4
METADATA_MAX_VALUES = 128


class MetadataRejected(ValueError):
    """Metadata cannot be retained without exceeding the supported snapshot contract."""


class _Snapshot:
    def __init__(self) -> None:
        self.output = bytearray()
        self.values = 0

    def append(self, data: bytes) -> None:
        if len(self.output) + len(data) > METADATA_LIMIT_BYTES:
            raise MetadataRejected("metadata exceeds the snapshot byte limit")
        self.output.extend(data)

    def value(self, value: Any, depth: int = 0) -> None:
        self.values += 1
        if depth > METADATA_MAX_DEPTH or self.values > METADATA_MAX_VALUES:
            raise MetadataRejected("metadata exceeds the depth or value limit")
        value_type = type(value)
        if value_type in (dict, list, tuple):
            self.container(value, depth)
            return
        if value_type is str:
            # A JSON string uses at least one byte per character. Check before allocating its encoding.
            if len(value) > METADATA_LIMIT_BYTES - len(self.output):
                raise MetadataRejected("metadata string exceeds the snapshot byte limit")
        elif value_type is int:
            # Bound integer conversion even when Python's optional decimal-digit guard is disabled.
            if value.bit_length() > METADATA_LIMIT_BYTES * 3:
                raise MetadataRejected("metadata integer exceeds the snapshot byte limit")
        elif value_type is float:
            if not math.isfinite(value):
                raise MetadataRejected("metadata contains a non-finite number")
        elif value_type not in (bool, type(None)):
            raise MetadataRejected("metadata contains an unsupported value type")
        try:
            encoded = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
        except ValueError as exc:
            raise MetadataRejected("metadata value cannot be encoded") from exc
        self.append(encoded)

    def container(self, value: Any, depth: int) -> None:
        if len(value) > METADATA_MAX_VALUES - self.values:
            raise MetadataRejected("metadata exceeds the value limit")
        mapping = type(value) is dict
        self.append(b"{" if mapping else b"[")
        for index, item in enumerate(value):
            if index:
                self.append(b",")
            if mapping:
                if type(item) is not str:
                    raise MetadataRejected("metadata keys must be strings")
                self.value(item, depth + 1)
                self.append(b":")
                item = value[item]
            self.value(item, depth + 1)
        self.append(b"}" if mapping else b"]")


def snapshot_metadata(source: str, name: str, fields: dict[str, Any]) -> bytes:
    """Snapshot supported primitive metadata without retaining mutable caller-owned objects.

    Work and allocation are bounded before visiting or encoding oversized values. Exact built-in types
    avoid invoking arbitrary serializers. The disk thread decodes this private snapshot for format writers.
    """
    snapshot = _Snapshot()
    try:
        snapshot.append(b'{"source":')
        snapshot.value(source)
        snapshot.append(b',"name":')
        snapshot.value(name)
        snapshot.append(b',"fields":')
        snapshot.value(fields)
        snapshot.append(b"}")
    except (RuntimeError, KeyError) as exc:
        raise MetadataRejected("metadata changed during snapshot creation") from exc
    return bytes(snapshot.output)


@dataclass(frozen=True)
class RetainedEntry:
    """An entry's raw bytes and private immutable metadata, charged until writing finishes."""

    kind: str
    wall: datetime
    offset: float
    data: bytes
    metadata: bytes
    ordinary: bool = True

    def decoded(self) -> Entry:
        metadata = json.loads(self.metadata)
        return Entry(self.kind, self.wall, self.offset, self.data, **metadata)
