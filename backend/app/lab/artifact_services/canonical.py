"""Canonical JSON helpers shared by Artifact commands and receipts."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


MAX_CANONICAL_BYTES = 256 * 1024


class CanonicalEncodingError(ValueError):
    pass


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_json_bytes(
    value: Any, *, max_bytes: int = MAX_CANONICAL_BYTES
) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    try:
        encoded = json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalEncodingError("value is not canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise CanonicalEncodingError("canonical JSON exceeds the byte limit")
    return encoded


def canonical_digest(value: Any, *, max_bytes: int = MAX_CANONICAL_BYTES) -> str:
    return hashlib.sha256(
        canonical_json_bytes(value, max_bytes=max_bytes)
    ).hexdigest()
