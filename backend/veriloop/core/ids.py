"""Short, URL-safe, time-sortable ids.

Job id = 9 chars of millisecond timestamp + 7 random chars, lowercase Crockford base32 (16 chars).
Lexicographic order == creation order (to the millisecond), ids are safe in URLs and file paths,
and 35 random bits per millisecond make collisions practically impossible at our scale.
"""

from __future__ import annotations

import re
import secrets
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32 (no i, l, o, u)
_TIME_CHARS = 9  # 45 bits of milliseconds -> good until the year 3084
_RAND_CHARS = 7
JOB_ID_RE = re.compile("^[" + _ALPHABET + "]{" + str(_TIME_CHARS + _RAND_CHARS) + "}$")


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def new_job_id(now_ms: int | None = None) -> str:
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    return _encode(ms, _TIME_CHARS) + _encode(secrets.randbits(5 * _RAND_CHARS), _RAND_CHARS)


def is_job_id(value: str) -> bool:
    return bool(JOB_ID_RE.fullmatch(value))


def job_id_timestamp_ms(job_id: str) -> int:
    value = 0
    for ch in job_id[:_TIME_CHARS]:
        value = value * 32 + _ALPHABET.index(ch)
    return value


def new_id(prefix: str = "") -> str:
    """Generic sortable id for other records (report attempts, legal drafts): `<prefix>_<job-style id>`."""
    return f"{prefix}_{new_job_id()}" if prefix else new_job_id()
