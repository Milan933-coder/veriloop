"""Evidence-table rendering and the Tool-MAD role split (pure functions, no I/O).

Proponent "tools" (SPEC §5): news RSS, wire, GDELT, Bing, Wikipedia/Wikidata -> ordinary news items.
Opponent "tools": Fact Check API / ClaimReview + forensic + provenance -> fact-check items and the
provenance branch's citable pages (P..). Forensic results carry no evidence id, so they are shown as
context only and can never ground a sentence on their own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from veriloop.contracts import (
    Argument,
    ClaimExtraction,
    EvidenceItem,
    ForensicResult,
    FusionResult,
    ProvenanceResult,
)

SNIPPET_CHARS = 420
MAX_TABLE_ITEMS = 32
_ID = re.compile(r"\b([EPD])\s*0*(\d{1,3})\b", re.IGNORECASE)
_FACTCHECK_TOOLS = frozenset({"claimreview_feed", "factcheck"})


def normalize_id(raw: str) -> str | None:
    """'[e3]' / 'E 3' / 'E03' -> 'E3'; anything else -> None."""
    m = _ID.search(raw or "")
    return f"{m.group(1).upper()}{int(m.group(2))}" if m and _ID.fullmatch(raw.strip(" []().,;")) else None


def inline_ids(text: str) -> list[str]:
    """Evidence ids written inside a sentence, e.g. 'PIB confirms it [E1, E4]'."""
    return [f"{m.group(1).upper()}{int(m.group(2))}" for m in _ID.finditer(text or "")]


def claim_text(extraction: ClaimExtraction) -> str:
    claim = extraction.primary_claim()
    if claim is None:
        return ""
    if claim.text_en and claim.text_en.strip() != claim.text.strip():
        return f"{claim.text} (English: {claim.text_en})"
    return claim.text


def is_opponent_item(item: EvidenceItem) -> bool:
    return (
        item.branch == "provenance"
        or item.id.startswith("P")
        or item.is_fact_check
        or item.claimreview is not None
        or item.tool in _FACTCHECK_TOOLS
    )


def split_roles(items: Sequence[EvidenceItem]) -> tuple[list[EvidenceItem], list[EvidenceItem]]:
    """(proponent items, opponent items)."""
    pro = [i for i in items if not is_opponent_item(i)]
    con = [i for i in items if is_opponent_item(i)]
    return pro, con


def render_item(item: EvidenceItem) -> str:
    flags = []
    if item.is_fact_check:
        flags.append("fact-check")
    if item.is_government_source:
        flags.append("government source")
    date = item.published_at.date().isoformat() if item.published_at else "undated"
    head = (
        f"[{item.id}] {item.source_name} | group: {item.publisher_group} | {date} | via {item.tool}"
        + (f" | {', '.join(flags)}" if flags else "")
        + f" | stance: {item.stance.value}"
    )
    lines = [head]
    if item.title and item.title.strip() != item.snippet.strip():
        lines.append(f"TITLE: {item.title}")
    lines.append(f"TEXT: {item.snippet[:SNIPPET_CHARS]}")
    cr = item.claimreview
    if cr is not None:
        lines.append(
            f"RATING: '{cr.rating_text}' by {cr.publisher} (normalized {cr.normalized_rating:+.1f}, "
            f"similarity to our claim {cr.similarity:.2f}"
            + (", manipulation rating" if cr.manipulation_rating else "")
            + ")"
        )
    return "\n".join(lines)


def render_table(items: Iterable[EvidenceItem]) -> str:
    rows = [render_item(i) for i in list(items)[:MAX_TABLE_ITEMS]]
    return "\n\n".join(rows) if rows else "(no evidence items)"


def render_context(forensic: ForensicResult | None, provenance: ProvenanceResult | None) -> str:
    """Non-citable context lines (forensic scores, provenance summary)."""
    lines = []
    if forensic is not None:
        score = f"{forensic.ensemble_score:.2f}" if forensic.ensemble_score is not None else "n/a"
        lines.append(f"Forensic models: status {forensic.status.value}, ensemble P(fake) {score}.")
    if provenance is not None:
        for m in provenance.known_fake_matches[:3]:
            lines.append(f"Known-fake index match {m.known_fake_id} labelled '{m.label}'.")
        if provenance.out_of_context_score > 0:
            lines.append(f"Provenance out-of-context score {provenance.out_of_context_score:.2f}.")
    return "\n".join(lines) or "(none)"


def render_fusion(fusion: FusionResult) -> str:
    signals = "; ".join(
        f"{s.name}={s.score:+.2f}" + ("" if s.present else " (absent)") for s in fusion.signals
    )
    return (
        f"candidate {fusion.candidate.value}"
        + (f" ({fusion.candidate_reason.value})" if fusion.candidate_reason else "")
        + f"; truth score T={fusion.truth_score:+.2f}; calibrated P(true)={fusion.p_true:.2f}; "
        f"manipulation M={fusion.manipulation_score:.2f}; independent groups: support "
        f"{fusion.groups_support}, refute {fusion.groups_refute}, manipulation {fusion.groups_manipulation}; "
        f"evidence state {fusion.evidence_state.value}; signals: {signals}"
    )


def render_argument(arg: Argument | None, role: str) -> str:
    if arg is None:
        return f"{role.upper()}: (no argument)"
    if not arg.points:
        return f"{role.upper()}: (no points)"
    pts = "\n".join(f"- {p.text} [{', '.join(p.evidence_ids)}]" for p in arg.points)
    return f"{role.upper()}:\n{pts}"
