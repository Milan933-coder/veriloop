from datetime import datetime

from pydantic import Field

from .common import Contract, ToolError, utcnow
from .enums import BranchStatus, RetrievalMethod, Stance


class ClaimReviewInfo(Contract):
    publisher: str
    rating_text: str  # textualRating as published
    normalized_rating: float  # -1 (false) .. +1 (true); 0 = mixed / unrated
    manipulation_rating: bool = False  # rating says altered / doctored / deepfake / edited
    claim_text: str | None = None
    claimant: str | None = None
    similarity: float = 0.0  # 0..1, claim vs reviewed claim


class EvidenceItem(Contract):
    """One citable piece of evidence. Every field is copied from a tool response; URLs are never synthesized."""

    id: str  # unique within a job: E1.. (evidence), P1.. (provenance), D1.. (debate follow-up)
    source_name: str
    url: str
    domain: str  # registrable domain, e.g. "reuters.com"
    publisher_group: str  # independence key: syndicated copies share a group
    title: str | None = None
    snippet: str
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    language: str | None = None
    confidence_relevance: float = 0.0  # 0..1
    stance: Stance = Stance.UNKNOWN
    stance_confidence: float = 0.0
    stance_quote: str | None = None  # must be an exact substring of snippet
    retrieval_method: RetrievalMethod
    tool: str
    tool_version: str = ""
    branch: str = "evidence"  # evidence|provenance|debate
    is_fact_check: bool = False
    is_government_source: bool = False
    claimreview: ClaimReviewInfo | None = None


class BranchResult(Contract):
    branch: str  # evidence|forensic|provenance
    status: BranchStatus
    items: list[EvidenceItem] = []
    errors: list[ToolError] = []
    tools_used: dict[str, str] = {}  # tool -> version
    latency_ms: int = 0
    notes: list[str] = []
    fast_path_hit: bool = False  # >=2 independent agreeing ClaimReviews with similarity >= threshold
