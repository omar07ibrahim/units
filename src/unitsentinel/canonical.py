"""Canonical byte primitives shared by content-addressed contracts."""

from __future__ import annotations

import hashlib
import json


def canonical_json_bytes(value: object) -> bytes:
    """Encode a JSON-compatible value into deterministic UTF-8 bytes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    """Return a lowercase SHA-256 digest for an exact byte sequence."""

    return hashlib.sha256(value).hexdigest()
