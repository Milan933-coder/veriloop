"""Canonical JSON + SHA-256 helpers.

`canonical_json` follows the audit rules (BUILD_SPEC §8): sorted keys, compact separators,
ensure_ascii=False, datetimes as ISO-8601 UTC, floats rounded to 6 dp. ToolClient uses it for cache
keys and cassette names so the same logical request always maps to the same file.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel


def normalize_for_json(obj: Any) -> Any:
    """Convert to plain JSON types deterministically (ValueError for NaN/inf, TypeError for unknown types)."""
    if obj is None or isinstance(obj, bool | str | int):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("non-finite float is not canonical JSON")
        return round(obj, 6)
    if isinstance(obj, Enum):
        return normalize_for_json(obj.value)
    if isinstance(obj, datetime):
        dt = obj if obj.tzinfo else obj.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return normalize_for_json(obj.model_dump(mode="json"))
    if isinstance(obj, Mapping):
        return {str(k): normalize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [normalize_for_json(v) for v in obj]
    if isinstance(obj, set | frozenset):
        return sorted((normalize_for_json(v) for v in obj), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(obj, Decimal):
        return normalize_for_json(float(obj))
    if isinstance(obj, PurePath):
        return str(obj)
    if hasattr(obj, "tolist"):  # numpy arrays / scalars, without importing numpy here
        return normalize_for_json(obj.tolist())
    raise TypeError(f"not JSON-serializable: {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(
        normalize_for_json(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_hex(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def stable_id(*parts: str, length: int = 32) -> str:
    """Deterministic id from text parts (e.g. ClaimReview url + claim text)."""
    return sha256_hex("\x1f".join(parts))[:length]
