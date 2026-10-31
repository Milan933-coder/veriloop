"""Signal computation for fusion (BUILD_SPEC §7). Pure functions over contract objects.

Independence: every count here is over `publisher_group` (syndicated copies already share a group,
set by the evidence branch). Aggregator groups ("aggregator:*", e.g. an MSN page whose original
publisher is unknown) are dropped from independence counting (data/lists/syndication.json rule c).
Wikidata items never count as publisher groups: they feed only the wikidata signal (entity facts are
not corroboration of an event).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from veriloop.contracts import (
    EvidenceItem,
    ForensicResult,
    ForensicStatus,
    ProvenanceResult,
    SignalScore,
    Stance,
)
from veriloop.services.evidence.lexicon import DETECTED_SUFFIX, has_manipulation_words
from veriloop.services.evidence.lists import factchecker_for, is_aggregator_group, publisher_group
from veriloop.services.fusion.config import FusionConfig

ENTITY_TOOLS = frozenset({"wikidata"})


def group_of(item: EvidenceItem) -> str:
    return item.publisher_group or item.domain or item.url


def countable(item: EvidenceItem) -> bool:
    return item.tool not in ENTITY_TOOLS and not is_aggregator_group(group_of(item))


@dataclass
class GroupTally:
    """Net stance per independent publisher group."""

    support: dict[str, list[EvidenceItem]] = field(default_factory=dict)
    refute: dict[str, list[EvidenceItem]] = field(default_factory=dict)
    neutral: dict[str, list[EvidenceItem]] = field(default_factory=dict)  # on-topic, no decisive stance
    confidence: dict[str, float] = field(default_factory=dict)  # decisive groups: mean stance confidence

    @property
    def groups_support(self) -> int:
        return len(self.support)

    @property
    def groups_refute(self) -> int:
        return len(self.refute)


def tally_groups(items: Iterable[EvidenceItem], cfg: FusionConfig) -> GroupTally:
    support_w: dict[str, float] = {}
    refute_w: dict[str, float] = {}
    members: dict[str, list[EvidenceItem]] = {}
    best_relevance: dict[str, float] = {}
    for it in items:
        if not countable(it):
            continue
        g = group_of(it)
        members.setdefault(g, []).append(it)
        best_relevance[g] = max(best_relevance.get(g, 0.0), it.confidence_relevance)
        weight = max(it.stance_confidence, 0.05)  # a stance with no stated confidence still counts
        if it.stance is Stance.SUPPORT:
            support_w[g] = support_w.get(g, 0.0) + weight
        elif it.stance is Stance.REFUTE:
            refute_w[g] = refute_w.get(g, 0.0) + weight
    tally = GroupTally()
    for g, group_items in members.items():
        s, r = support_w.get(g, 0.0), refute_w.get(g, 0.0)
        if s > r:
            picked = [i for i in group_items if i.stance is Stance.SUPPORT]
            tally.support[g] = picked
        elif r > s:
            picked = [i for i in group_items if i.stance is Stance.REFUTE]
            tally.refute[g] = picked
        else:
            # Tie (or no stance): the group is on-topic but not decisive. Only relevant groups dilute.
            classified = [i for i in group_items if i.stance is not Stance.UNKNOWN]
            if classified and best_relevance[g] >= cfg.p("neutral_relevance_min"):
                tally.neutral[g] = classified
            continue
        tally.confidence[g] = sum(i.stance_confidence for i in picked) / len(picked)
    return tally


def news_stance_signal(tally: GroupTally, cfg: FusionConfig) -> SignalScore:
    gs, gr = tally.groups_support, tally.groups_refute
    stance_groups = gs + gr + len(tally.neutral)
    weight = cfg.weights["news_stance"]
    if stance_groups == 0:
        return SignalScore(
            name="news_stance", score=0.0, weight=weight, present=False, detail="no stance groups"
        )
    decisive = list(tally.confidence.values())
    reliability = max(cfg.r("news_stance_floor"), sum(decisive) / len(decisive)) if decisive else 1.0
    ids = [i.id for items in (*tally.support.values(), *tally.refute.values()) for i in items]
    return SignalScore(
        name="news_stance",
        score=(gs - gr) / max(1, stance_groups),
        weight=weight,
        reliability=round(min(1.0, reliability), 6),
        detail=f"support_groups={gs} refute_groups={gr} neutral_groups={len(tally.neutral)}",
        evidence_ids=sorted(set(ids), key=_id_key),
    )


@dataclass
class ReviewMatch:
    item: EvidenceItem
    direction: float  # signed rating relative to OUR claim, in [-1, 1]
    similarity: float
    reliability: float


def claimreview_matches(items: Iterable[EvidenceItem], cfg: FusionConfig) -> list[ReviewMatch]:
    """Best review per independent group with similarity >= threshold that addresses this claim.

    `stance` was set by the evidence branch relative to OUR claim: neutral means "a review of a
    different claim" and is skipped; support/refute fix the sign (the reviewed claim may be the
    negation of ours); unknown (no LLM/NLI) falls back to the raw rating.
    """
    min_sim = cfg.p("claimreview_min_similarity")
    best: dict[str, ReviewMatch] = {}
    for it in items:
        cr = it.claimreview
        if cr is None or cr.similarity < min_sim or it.stance is Stance.NEUTRAL:
            continue
        if is_aggregator_group(group_of(it)):
            continue
        magnitude = abs(cr.normalized_rating)
        if it.stance is Stance.SUPPORT:
            direction = magnitude
        elif it.stance is Stance.REFUTE:
            direction = -magnitude
        else:
            direction = cr.normalized_rating
        checker = factchecker_for(it.domain or it.url, url=it.url, title=it.title or "")
        rel = checker.reliability if checker else cfg.r("claimreview_unknown_publisher")
        rel *= (
            cfg.r("claimreview_headline")
            if cr.rating_text.endswith(DETECTED_SUFFIX)
            else cfg.r("claimreview_published")
        )
        match = ReviewMatch(it, direction, cr.similarity, rel)
        g = group_of(it)
        if g not in best or match.similarity > best[g].similarity:
            best[g] = match
    return sorted(best.values(), key=lambda m: (-m.similarity, _id_key(m.item.id)))


def claimreview_signal(matches: Sequence[ReviewMatch], cfg: FusionConfig) -> SignalScore:
    weight = cfg.weights["claimreview"]
    if not matches:
        return SignalScore(
            name="claimreview",
            score=0.0,
            weight=weight,
            present=False,
            detail="no ClaimReview with similarity >= threshold (absence is not evidence: E4)",
        )
    score = sum(m.direction * m.similarity for m in matches) / len(matches)
    reliability = sum(m.reliability for m in matches) / len(matches)
    return SignalScore(
        name="claimreview",
        score=_clip(score),
        weight=weight,
        reliability=round(reliability, 6),
        detail="; ".join(
            f"{m.item.id} {m.item.claimreview.publisher if m.item.claimreview else ''}: "
            f"'{m.item.claimreview.rating_text if m.item.claimreview else ''}' sim={m.similarity:.2f}"
            for m in matches
        ),
        evidence_ids=[m.item.id for m in matches],
    )


def fast_path(matches: Sequence[ReviewMatch], cfg: FusionConfig) -> bool:
    """>= 2 independent ClaimReview matches at similarity >= fast_path_similarity that agree in sign."""
    strong = [m for m in matches if m.similarity >= cfg.p("fast_path_similarity") and m.direction != 0]
    pos = {group_of(m.item) for m in strong if m.direction > 0}
    neg = {group_of(m.item) for m in strong if m.direction < 0}
    return (len(pos) >= 2 and not neg) or (len(neg) >= 2 and not pos)


def gdelt_signal(items: Iterable[EvidenceItem], cfg: FusionConfig) -> SignalScore:
    weight = cfg.weights["gdelt"]
    hits = [
        i
        for i in items
        if i.tool == "gdelt" and i.confidence_relevance >= cfg.p("gdelt_relevance_min") and countable(i)
    ]
    groups = {group_of(i) for i in hits}
    if not groups:
        return SignalScore(
            name="gdelt",
            score=0.0,
            weight=weight,
            present=False,
            detail="no GDELT coverage (absent, not negative)",
        )
    return SignalScore(
        name="gdelt",
        score=min(1.0, len(groups) / cfg.p("gdelt_saturation_groups")),
        weight=weight,
        reliability=cfg.r("gdelt"),
        detail=f"{len(groups)} independent GDELT publisher group(s)",
        evidence_ids=sorted({i.id for i in hits}, key=_id_key),
    )


def wikidata_signal(items: Iterable[EvidenceItem], cfg: FusionConfig) -> SignalScore:
    weight = cfg.weights["wikidata"]
    wd = [i for i in items if i.tool == "wikidata"]
    contradicted = [i for i in wd if i.stance is Stance.REFUTE]
    consistent = [i for i in wd if i.stance is Stance.SUPPORT]
    if contradicted:
        return SignalScore(
            name="wikidata",
            score=-1.0,
            weight=weight,
            reliability=cfg.r("wikidata_contradicted"),
            detail="Wikidata contradicts the claim: "
            + "; ".join(i.stance_quote or i.snippet[:80] for i in contradicted),
            evidence_ids=[i.id for i in contradicted],
        )
    if consistent:
        return SignalScore(
            name="wikidata",
            score=1.0,
            weight=weight,
            reliability=cfg.r("wikidata_consistent"),
            detail="Wikidata is consistent with the claim's entities/offices",
            evidence_ids=[i.id for i in consistent],
        )
    return SignalScore(
        name="wikidata", score=0.0, weight=weight, present=False, detail="no Wikidata check applied"
    )


def provenance_signal(provenance: ProvenanceResult | None, cfg: FusionConfig) -> SignalScore:
    weight = cfg.weights["provenance"]
    if provenance is not None:
        fakes = [m for m in provenance.known_fake_matches if m.label in ("manipulated", "out_of_context")]
        if fakes:
            return SignalScore(
                name="provenance",
                score=-1.0,
                weight=weight,
                reliability=cfg.r("provenance"),
                detail="known-fake match: " + ", ".join(f"{m.known_fake_id} ({m.label})" for m in fakes),
            )
        if provenance.out_of_context_score > 0:
            return SignalScore(
                name="provenance",
                score=-0.7 * min(1.0, provenance.out_of_context_score),
                weight=weight,
                reliability=cfg.r("provenance"),
                detail=f"out_of_context_score={provenance.out_of_context_score:.2f}",
            )
    return SignalScore(
        name="provenance", score=0.0, weight=weight, present=False, detail="no provenance signal"
    )


def forensic_signal(forensic: ForensicResult | None) -> SignalScore:
    """Informational only (manipulation axis M, not part of T)."""
    if forensic is None or forensic.ensemble_score is None:
        status = forensic.status.value if forensic else "absent"
        return SignalScore(name="forensic", score=0.0, weight=0.0, present=False, detail=f"forensic {status}")
    return SignalScore(
        name="forensic",
        score=forensic.ensemble_score,
        weight=0.0,
        present=forensic.status in (ForensicStatus.MANIPULATED, ForensicStatus.REAL),
        detail=(
            f"P(fake)={forensic.ensemble_score:.2f} status={forensic.status.value} (manipulation axis M only)"
        ),
    )


def truth_score(signals: Sequence[SignalScore]) -> float:
    present = [s for s in signals if s.present and s.weight > 0]
    total = sum(s.weight for s in present)
    if total <= 0:
        return 0.0
    return _clip(sum(s.weight * s.score * s.reliability for s in present) / total)


@dataclass
class Manipulation:
    score: float  # M in [0, 1]
    groups: dict[str, list[str]]  # group -> evidence ids (or source urls) documenting manipulation
    forensic_manipulated: bool
    known_fake_manipulated: bool
    claimreview_manipulated: bool
    evidence_ids: list[str]


def manipulation_axis(
    items: Sequence[EvidenceItem],
    forensic: ForensicResult | None,
    provenance: ProvenanceResult | None,
    cfg: FusionConfig,
) -> Manipulation:
    min_sim = cfg.p("claimreview_min_similarity")
    groups: dict[str, list[str]] = {}
    cr_manip = False
    for it in items:
        if not countable(it):
            continue
        cr = it.claimreview
        documented = False
        if (
            cr is not None
            and cr.manipulation_rating
            and cr.similarity >= min_sim
            and it.stance is not Stance.NEUTRAL
        ):
            documented = cr_manip = True
        elif it.stance is Stance.REFUTE and has_manipulation_words(
            f"{it.stance_quote or ''} {it.title or ''}"
        ):
            documented = True  # e.g. "Indian Army debunks deepfake video" (news report of a debunk)
        if documented:
            groups.setdefault(group_of(it), []).append(it.id)
    km_manip = False
    if provenance is not None:
        by_url = {i.url: i for i in items}
        for match in provenance.known_fake_matches:
            if match.label != "manipulated":
                continue
            km_manip = True
            for url in match.source_urls:
                item = by_url.get(url)
                g = group_of(item) if item else publisher_group(url)
                if not is_aggregator_group(g):
                    groups.setdefault(g, []).append(item.id if item else url)
    forensic_manip = forensic is not None and forensic.status is ForensicStatus.MANIPULATED
    m = 0.0
    if (
        forensic is not None
        and forensic.ensemble_score is not None
        and forensic.status
        in (
            ForensicStatus.MANIPULATED,
            ForensicStatus.REAL,
        )
    ):
        m = max(m, float(forensic.ensemble_score))  # "conclusive" ensemble only
    if km_manip:
        m = max(m, cfg.p("manipulation_known_fake_score"))
    if cr_manip:
        m = max(m, cfg.p("manipulation_claimreview_score"))
    ids = sorted({i for v in groups.values() for i in v if not i.startswith("http")}, key=_id_key)
    return Manipulation(round(min(1.0, m), 6), groups, forensic_manip, km_manip, cr_manip, ids)


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _id_key(item_id: str) -> tuple[str, int, str]:
    prefix = item_id.rstrip("0123456789")
    digits = item_id[len(prefix) :]
    return (prefix, int(digits) if digits.isdigit() else 0, item_id)


id_sort_key = _id_key
