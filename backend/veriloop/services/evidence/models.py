"""Internal working types of the evidence branch (not contracts: they never leave this package)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from veriloop.contracts import ClaimReviewInfo, RetrievalMethod, Stance, ToolError


@dataclass
class Candidate:
    """One search hit before it becomes an EvidenceItem. Every field is copied from a tool response."""

    source_name: str
    url: str  # as returned by the tool (Google News items: the news.google.com article link)
    host: str  # publisher host (Google News: <source url>; Bing: the url= target)
    title: str
    snippet: str
    retrieval_method: RetrievalMethod
    tool: str
    published_at: datetime | None = None
    language: str | None = None
    gnews_link: str | None = None  # encoded Google News link, decoded lazily for the kept items
    claimreview: ClaimReviewInfo | None = None
    rating_method: str | None = None  # feed | factcheck_api | jsonld | headline
    is_fact_check: bool = False
    is_government: bool = False
    pinned: bool = False  # keep regardless of relevance rank (ClaimReview matches, Wikidata facts)
    stance: Stance = Stance.UNKNOWN
    stance_confidence: float = 0.0
    stance_quote: str | None = None
    relevance: float = 0.0
    embedding: np.ndarray | None = None
    group: str = ""
    domain: str = ""
    item_id: str = ""


@dataclass
class SourceOutcome:
    tool: str
    op: str
    candidates: list[Candidate] = field(default_factory=list)
    error: ToolError | None = None
    note: str | None = None
    returned_urls: set[str] = field(default_factory=set)  # every URL this tool response contained
    latency_ms: int = 0
    from_cache: bool = False
