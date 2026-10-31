"""Streaming parser for Google's public ClaimReview DataFeed (D-15; docs/research.md "ClaimReview feed").

The feed is ~200 MB of pretty-printed JSON. `json.load` needs several GB of RAM (the dev laptop has
~2 GB free), so `iter_feed_elements` walks the `dataFeedElement` array with `raw_decode`, keeping one
element in memory at a time. `parse_element` turns one DataFeedItem into ClaimReviewRecords and
guards the data-quality gaps documented in research.md: `item: null`, `item` as a dict instead of a
list, garbage dates (`710-01-01`, `2923-01-02`), claimants as strings/dicts/lists, missing
`claimReviewed`, publisher-specific numeric scales (kept but never used for the verdict).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dateutil import parser as dateparser

from veriloop.db.repo import ClaimReviewRecord
from veriloop.services.evidence.lists import host_of

_CHUNK = 1 << 20
_MIN_DATE = datetime(1995, 1, 1, tzinfo=UTC)
_WS = " \t\r\n"


def iter_feed_elements(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each element of the top-level `dataFeedElement` array without loading the whole file."""
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as fh:
        buf = ""
        # 1) find the array start
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                return
            buf += chunk
            m = re.search(r'"dataFeedElement"\s*:\s*\[', buf)
            if m:
                buf = buf[m.end() :]
                break
            buf = buf[-64:]  # the key may straddle a chunk boundary
        pos = 0
        eof = False
        while True:
            # skip whitespace and separators
            while True:
                while pos < len(buf) and buf[pos] in _WS + ",":
                    pos += 1
                if pos < len(buf) or eof:
                    break
                chunk = fh.read(_CHUNK)
                eof = not chunk
                buf, pos = buf[pos:] + chunk, 0
            if pos >= len(buf) or buf[pos] == "]":
                return
            try:
                obj, end = decoder.raw_decode(buf, pos)
            except json.JSONDecodeError:
                if eof:
                    raise
                chunk = fh.read(_CHUNK)
                eof = not chunk
                buf, pos = buf[pos:] + chunk, 0
                continue
            pos = end
            if isinstance(obj, dict):
                yield obj
            if pos > _CHUNK:  # drop consumed text so the buffer stays small
                buf, pos = buf[pos:], 0


def parse_date(value: Any, *, now: datetime | None = None) -> datetime | None:
    """Validated datetime (UTC) or None for garbage like '710-01-01' / '2923-01-02'."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        dt = dateparser.isoparse(text)
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    upper = (now or datetime.now(UTC)) + timedelta(days=2)
    return dt if _MIN_DATE <= dt <= upper else None


def _name(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    if isinstance(value, list):
        for v in value:
            n = _name(v)
            if n:
                return n
    return None


def _urls(value: Any) -> list[str]:
    out: list[str] = []
    items = value if isinstance(value, list) else [value]
    for v in items:
        url = v.get("url") if isinstance(v, dict) else v if isinstance(v, str) else None
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            out.append(url.strip())
    return out


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


_SCRIPTS = (
    (re.compile(r"[ऀ-ॿ]"), "hi"),
    (re.compile(r"[ঀ-৿]"), "bn"),
    (re.compile(r"[਀-੿]"), "pa"),
    (re.compile(r"[઀-૿]"), "gu"),
    (re.compile(r"[஀-௿]"), "ta"),
    (re.compile(r"[ఀ-౿]"), "te"),
    (re.compile(r"[ಀ-೿]"), "kn"),
    (re.compile(r"[ഀ-ൿ]"), "ml"),
    (re.compile(r"[؀-ۿ]"), "ar"),
)


def script_language(text: str) -> str | None:
    """Cheap language hint from the writing system (the feed has no inLanguage). Latin -> None.

    langdetect over 100k rows costs minutes; the column is informational (FTS does not filter on it).
    Devanagari is reported as 'hi' (Marathi/Nepali share the script).
    """
    for pattern, code in _SCRIPTS:
        if pattern.search(text or ""):
            return code
    return None


def parse_element(element: dict[str, Any], *, now: datetime | None = None) -> list[ClaimReviewRecord]:
    items = element.get("item")
    if items is None:
        return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    out: list[ClaimReviewRecord] = []
    for review in items:
        if not isinstance(review, dict):
            continue
        claim = review.get("claimReviewed")
        url = review.get("url") or element.get("url")
        if not isinstance(claim, str) or not claim.strip() or not isinstance(url, str):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        author = review.get("author") if isinstance(review.get("author"), dict) else {}
        rating = review.get("reviewRating") if isinstance(review.get("reviewRating"), dict) else {}
        reviewed = review.get("itemReviewed") if isinstance(review.get("itemReviewed"), dict) else {}
        site = author.get("url") if isinstance(author.get("url"), str) else None
        appearances = _urls(reviewed.get("appearance")) + _urls(reviewed.get("firstAppearance"))
        rating_text = rating.get("alternateName")
        claim_text = " ".join(claim.split())
        out.append(
            ClaimReviewRecord(
                claim_text=claim_text[:4000],
                claimant=_name(reviewed.get("author")),
                claim_date=parse_date(reviewed.get("datePublished"), now=now),
                review_url=url.strip(),
                publisher_name=_name(author),
                publisher_site=host_of(site) if site else host_of(url),
                rating_text=rating_text.strip()[:500] if isinstance(rating_text, str) else None,
                rating_value=_float(rating.get("ratingValue")),
                best_rating=_float(rating.get("bestRating")),
                worst_rating=_float(rating.get("worstRating")),
                language=script_language(claim_text),
                review_date=parse_date(review.get("datePublished"), now=now),
                item_appearance_urls=list(dict.fromkeys(appearances))[:20],
                image_url=None,  # the feed has no item-level image (research.md)
            )
        )
    return out
