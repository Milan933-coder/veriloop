"""First-seen estimate and out-of-context scoring (pure functions).

out_of_context: the earliest *other* appearance of this media predates the event the claim describes.
The claimed event date comes from an explicit date in the claim/time reference; when the claim states
none, an "event" claim is read as describing something current (the submission time) and the score is
discounted, because that reading is an assumption.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from veriloop.contracts import Appearance, ClaimExtraction

OOC_MIN_GAP_DAYS = 7  # fact-checks lag virality by days; smaller gaps say nothing
IMPLICIT_DISCOUNT = 0.7

_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for m in names
}
_ISO = re.compile(r"\b(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})\b")
_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_MY = re.compile(r"\b([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")  # India: day first
_YEAR = re.compile(r"\b(19[5-9]\d|20\d{2})\b")
_RELATIVE = re.compile(r"\b(today|tonight|yesterday|this (morning|week)|just now|right now|breaking)\b", re.I)


def _safe(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def parse_event_date(text: str | None, now: datetime) -> datetime | None:
    """Earliest plausible date the text explicitly refers to (month/year-only -> first day)."""
    if not text:
        return None
    if m := _ISO.search(text):
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if (m := _DMY.search(text)) and m.group(2).lower() in _MONTHS:
        return _safe(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    if (m := _MDY.search(text)) and m.group(1).lower() in _MONTHS:
        return _safe(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    if m := _NUMERIC.search(text):
        return _safe(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if (m := _MY.search(text)) and m.group(1).lower() in _MONTHS:
        return _safe(int(m.group(2)), _MONTHS[m.group(1).lower()], 1)
    if _RELATIVE.search(text):
        return now - timedelta(days=1)
    if m := _YEAR.search(text):
        return _safe(int(m.group(1)), 1, 1)
    return None


def claimed_event_date(extraction: ClaimExtraction, now: datetime) -> tuple[datetime | None, bool]:
    """(date, explicit). Implicit: an event claim with no date is taken to describe a current event."""
    claim = extraction.primary_claim()
    if claim is None:
        return None, False
    for text in (claim.time_reference, claim.text_en, claim.text):
        found = parse_event_date(text, now)
        if found is not None and found <= now + timedelta(days=2):
            return found, True
    if claim.claim_type == "event":
        return now, False
    return None, False


def earliest(appearances: list[Appearance]) -> Appearance | None:
    dated = [a for a in appearances if a.first_seen_at is not None]
    return min(dated, key=lambda a: a.first_seen_at) if dated else None  # type: ignore[arg-type,return-value]


def out_of_context_score(first_seen: datetime | None, event: datetime | None, *, explicit: bool) -> float:
    """0 when unknown or within OOC_MIN_GAP_DAYS; rises with the gap (0.5 at a week, 1.0 by ~2 months)."""
    if first_seen is None or event is None:
        return 0.0
    gap_days = (event - first_seen).total_seconds() / 86400.0
    if gap_days <= OOC_MIN_GAP_DAYS:
        return 0.0
    score = min(1.0, 0.5 + gap_days / 120.0)
    if not explicit:
        score *= IMPLICIT_DISCOUNT
    return round(score, 3)
