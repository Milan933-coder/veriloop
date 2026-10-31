"""Builders for reasoning tests (fusion + debate). Plain functions, no fixtures."""

from __future__ import annotations

import math
from typing import Any

from veriloop.contracts import (
    AbstainBand,
    BranchResult,
    BranchStatus,
    Claim,
    ClaimExtraction,
    ClaimReviewInfo,
    EvidenceItem,
    ForensicResult,
    ForensicStatus,
    KnownFakeMatch,
    ProvenanceResult,
    RetrievalMethod,
    Stance,
    SubjectInfo,
    ToolError,
)
from veriloop.services.fusion.config import Calibration

CLAIM = "The Union Cabinet approved a 3% hike in Dearness Allowance on 28 October 2026."


def extraction(
    *,
    worthiness: float = 0.9,
    claims: bool = True,
    language: str = "en",
    text_en: str | None = None,
    identifiable: bool = False,
    private: bool | None = None,
    name: str | None = None,
) -> ClaimExtraction:
    claim_list = [Claim(id="C1", text=CLAIM, text_en=text_en)] if claims else []
    return ClaimExtraction(
        language=language,
        claims=claim_list,
        primary_claim_id="C1" if claims else None,
        check_worthiness=worthiness,
        worthiness_reason="test",
        subject=SubjectInfo(
            is_identifiable_individual=identifiable, is_private_individual=private, name=name
        ),
    )


def item(
    item_id: str,
    group: str,
    stance: Stance = Stance.NEUTRAL,
    *,
    conf: float = 0.9,
    relevance: float = 0.8,
    tool: str = "gnews_rss",
    title: str | None = None,
    snippet: str | None = None,
    quote: str | None = None,
    claimreview: ClaimReviewInfo | None = None,
    domain: str | None = None,
    url: str | None = None,
    is_fact_check: bool = False,
) -> EvidenceItem:
    domain = domain or (group if "." in group else f"{group}.example")
    title = title if title is not None else f"{group} report {item_id}"
    snippet = snippet if snippet is not None else f"{title}. Details of the report."
    if quote is None and stance in (Stance.SUPPORT, Stance.REFUTE):
        quote = title
    return EvidenceItem(
        id=item_id,
        source_name=group,
        url=url or f"https://{domain}/article/{item_id.lower()}",
        domain=domain,
        publisher_group=group,
        title=title,
        snippet=snippet,
        confidence_relevance=relevance,
        stance=stance,
        stance_confidence=conf if stance in (Stance.SUPPORT, Stance.REFUTE, Stance.NEUTRAL) else 0.0,
        stance_quote=quote,
        retrieval_method=RetrievalMethod.GOOGLE_NEWS_RSS,
        tool=tool,
        is_fact_check=is_fact_check or claimreview is not None,
        claimreview=claimreview,
    )


def review(
    item_id: str,
    group: str,
    *,
    rating: float = -1.0,
    similarity: float = 0.9,
    rating_text: str = "False",
    manipulation: bool = False,
    stance: Stance | None = None,
) -> EvidenceItem:
    if stance is None:
        stance = Stance.REFUTE if rating < 0 else Stance.SUPPORT if rating > 0 else Stance.NEUTRAL
    info = ClaimReviewInfo(
        publisher=group,
        rating_text=rating_text,
        normalized_rating=rating,
        manipulation_rating=manipulation,
        claim_text=CLAIM,
        similarity=similarity,
    )
    return item(
        item_id,
        group,
        stance,
        tool="claimreview_feed",
        title=f"Fact check by {group}",
        snippet=f"Claim reviewed: {CLAIM} Rating: {rating_text}",
        quote=f"Rating: {rating_text}",
        claimreview=info,
    )


def evidence(
    items: list[EvidenceItem], *, errors: list[ToolError] | None = None, status=BranchStatus.OK
) -> BranchResult:
    return BranchResult(branch="evidence", status=status, items=items, errors=errors or [])


def forensic(
    status: ForensicStatus = ForensicStatus.UNAVAILABLE, score: float | None = None
) -> ForensicResult:
    return ForensicResult(
        branch_status=BranchStatus.OK if status is not ForensicStatus.UNAVAILABLE else BranchStatus.SKIPPED,
        status=status,
        ensemble_score=score,
        band=AbstainBand(low=0.35, high=0.70, delta=0.40),
    )


def provenance(
    *,
    known_fake: str | None = None,
    source_urls: list[str] | None = None,
    out_of_context: float = 0.0,
    items: list[EvidenceItem] | None = None,
) -> ProvenanceResult:
    matches = []
    if known_fake:
        matches.append(
            KnownFakeMatch(known_fake_id="KF-T", label=known_fake, hamming=2, source_urls=source_urls or [])
        )
    return ProvenanceResult(
        branch_status=BranchStatus.OK,
        known_fake_matches=matches,
        out_of_context_score=out_of_context,
        items=items or [],
    )


def constant_calibration(p: float) -> Calibration:
    """Calibration whose p_true is `p` regardless of T (a=0, b=logit(p))."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    return Calibration(version="test-constant", a=0.0, b=math.log(p / (1 - p)))


def error(tool: str, error_type: str = "timeout") -> ToolError:
    return ToolError(tool=tool, op="search", error_type=error_type, message="boom")


def kwargs(**over: Any) -> dict[str, Any]:
    return over
