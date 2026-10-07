"""Canonical JSON for hashes that must be reproducible across processes and library versions
(docs/design/security.md 13.1 audit chain, 7.5 approval payload hashes).

Rules: keys sorted, no whitespace, UTF-8 (non-ASCII kept as is), values limited to str, int,
bool, None, list and dict. Floats are refused: their textual form is not stable enough to
hash, so timestamps are integers (milliseconds) and amounts are integers in minor units.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

VERSION = 1  # recorded in audit checkpoint rows so a future change cannot look like tampering


class CanonicalError(TypeError):
    pass


def _check(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise CanonicalError(f"float at {path} is not allowed in canonical JSON")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(f"non-string key at {path}")
            _check(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    raise CanonicalError(f"unsupported type {type(value).__name__} at {path}")


def canonical_dumps(value: Any) -> str:
    _check(value, "$")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_value(value: Any) -> str:
    """SHA-256 hex of the canonical JSON of value."""
    return sha256_hex(canonical_bytes(value))
