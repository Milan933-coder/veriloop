"""Test builders shared by the actions packages' tests (orchestrator, api, audit, reporting, legal, bots).

* `SESSION_AUDIT_KEY`: one Ed25519 key for the whole test session. Every actions test directory
  overrides the `settings` fixture to use it, because `verify_chain` checks the whole (shared) chain
  and rows signed with per-test keys would look forged.
* `item()`, `make_view()`: realistic contract objects without running the pipeline.
* `FakeServices`: a scriptable ServiceSet (each stage can sleep, raise or be missing).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from veriloop.contracts import (
    AbstainBand,
    AbstainReason,
    BranchResult,
    BranchStatus,
    Claim,
    ClaimExtraction,
    DebateResult,
    Entity,
    EvidenceItem,
    EvidenceState,
    ForensicResult,
    ForensicStatus,
    FusionResult,
    InputKind,
    InputSummary,
    JobStatus,
    JobView,
    JudgeOutput,
    MediaAsset,
    MediaKind,
    ModelScore,
    Platform,
    ProvenanceResult,
    RationaleSentence,
    ResolvedInput,
    RetrievalMethod,
    SignalScore,
    Stance,
    SubjectInfo,
    Verdict,
    VerdictLabel,
    VerifierOutput,
)
from veriloop.db.engine import Database
from veriloop.orchestrator.services import ServiceSet
from veriloop.services.audit.keys import generate_private_pem

SESSION_AUDIT_KEY = generate_private_pem()


@asynccontextmanager
async def fresh_database(db: Database, prefix: str) -> AsyncIterator[Database]:
    """A database of its own on the shared server, so whole-chain `verify_chain` checks never see rows
    other packages' tests wrote into the shared session database (e.g. raw, unsigned trigger-test rows)."""
    name = f"{prefix}_{secrets.token_hex(4)}"
    dsn = await db.create_database(name)
    fresh = await Database.connect(dsn=dsn, min_size=1, max_size=8)
    await fresh.apply_schema()
    try:
        yield fresh
    finally:
        await fresh.close()
        await db.drop_database(name)


T0 = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def item(
    id_: str,
    domain: str,
    *,
    group: str | None = None,
    stance: Stance = Stance.REFUTE,
    title: str | None = None,
    branch: str = "evidence",
    fact_check: bool = True,
) -> EvidenceItem:
    return EvidenceItem(
        id=id_,
        source_name=domain.split(".")[0].title(),
        url=f"https://{domain}/fact-check/{id_.lower()}",
        domain=domain,
        publisher_group=group or domain,
        title=title or f"Fact check {id_} by {domain}",
        snippet=f"{domain} found the viral video is old footage ({id_}).",
        published_at=T0,
        retrieved_at=T0,
        confidence_relevance=0.9,
        stance=stance,
        stance_confidence=0.9,
        retrieval_method=RetrievalMethod.GOOGLE_NEWS_RSS,
        tool="gnews_rss",
        tool_version="news.google.com rss",
        branch=branch,
        is_fact_check=fact_check,
    )


def band() -> AbstainBand:
    return AbstainBand(low=0.35, high=0.70, delta=0.40)


def forensic(
    status: ForensicStatus = ForensicStatus.INCONCLUSIVE, score: float | None = 0.5
) -> ForensicResult:
    return ForensicResult(
        branch_status=BranchStatus.OK,
        status=status,
        ensemble_score=score,
        disagreement=0.1,
        band=band(),
        models=[
            ModelScore(
                model_id="org/deepfake-detector",
                provider="local",
                raw_score=score,
                weight=1.0,
                frames_scored=4,
            )
        ],
        frames_analyzed=4,
    )


def extraction(
    *,
    text: str = "This video shows Cyclone Arnab hitting Visakhapatnam.",
    worthiness: float = 0.9,
    identifiable: bool = False,
    private: bool | None = None,
    name: str | None = None,
    claims: bool = True,
) -> ClaimExtraction:
    return ClaimExtraction(
        caption_text=text,
        language="en",
        claims=[Claim(id="C1", text=text, entities=[Entity(name="Visakhapatnam", type="place")])]
        if claims
        else [],
        primary_claim_id="C1" if claims else None,
        check_worthiness=worthiness,
        worthiness_reason="event claim with named place",
        subject=SubjectInfo(
            is_identifiable_individual=identifiable, is_private_individual=private, name=name
        ),
    )


def make_view(
    label: VerdictLabel = VerdictLabel.FALSE,
    *,
    job_id: str = "0000000000test01",
    source: str = "web",
    confidence: float = 0.86,
    identifiable: bool = False,
    private: bool | None = None,
    forensic_status: ForensicStatus = ForensicStatus.INCONCLUSIVE,
    groups_manipulation: int = 0,
    platform: Platform = Platform.X,
    url: str | None = "https://x.com/someone/status/123",
    audit_id: int | None = None,
    status: JobStatus = JobStatus.COMPLETED,
) -> JobView:
    items = [item("E1", "newschecker.in"), item("E2", "factcrescendo.com"), item("E3", "thequint.com")]
    abstain = label is VerdictLabel.UNVERIFIABLE
    view = JobView(
        job_id=job_id,
        status=status,
        created_at=T0,
        updated_at=T0,
        source=source,  # type: ignore[arg-type]
        verdict=label,
        confidence=confidence,
        abstain_reason=AbstainReason.LOW_CONFIDENCE if abstain else None,
        evidence_state=EvidenceState.MANIPULATED
        if label is VerdictLabel.MANIPULATED
        else EvidenceState.REFUTES
        if label is VerdictLabel.FALSE
        else EvidenceState.SUPPORTS
        if label is VerdictLabel.TRUE
        else EvidenceState.INSUFFICIENT,
        rationale=[
            RationaleSentence(
                text="Two fact-checkers report the video is old footage.", evidence_ids=["E1", "E2"]
            )
        ],
        cited_evidence_ids=["E1", "E2"],
        independent_source_count=2,
        sources=items,
        forensic=forensic(forensic_status, 0.9 if forensic_status is ForensicStatus.MANIPULATED else 0.5),
        provenance=ProvenanceResult(branch_status=BranchStatus.OK),
        claim=extraction(
            identifiable=identifiable, private=private, name="Jane Doe" if identifiable else None
        ),
        input=InputSummary(
            platform=platform,
            input_kind="url" if url else "text",
            url=url,
            title="Viral cyclone video",
            media_accessible=True,
            media_count=1,
            media_hashes=[
                {
                    "id": "M1",
                    "kind": "image",
                    "sha256": "ab" * 32,
                    "phash": "f0f0f0f0f0f0f0f0",
                    "frame_ts": None,
                }
            ],
        ),
        fusion=FusionResult(
            truth_score=-0.8,
            manipulation_score=0.9 if label is VerdictLabel.MANIPULATED else 0.1,
            p_true=0.14,
            signals=[SignalScore(name="news_stance", score=-1.0, weight=0.25, evidence_ids=["E1", "E2"])],
            groups_refute=3,
            groups_manipulation=groups_manipulation,
            candidate=label,
            evidence_state=EvidenceState.REFUTES,
            weights_version="w-test",
            calibration_version="cal-test",
            thresholds={"abstain_threshold": 0.7},
        ),
        report_eligible=label in (VerdictLabel.FALSE, VerdictLabel.MANIPULATED),
        audit_id=audit_id,
        resolved_models={"llm_primary": "fake-model"},
    )
    return view


# ---- fake service set ------------------------------------------------------------------------


class FakeServices:
    """Scriptable stage implementations. `delay` (s) per async stage; `fail` maps stage -> exception."""

    def __init__(
        self,
        *,
        label: VerdictLabel = VerdictLabel.FALSE,
        worthiness: float = 0.9,
        claims: bool = True,
        candidate: VerdictLabel | None = None,
        delay: float = 0.0,
        fail: dict[str, BaseException] | None = None,
        identifiable: bool = False,
        private: bool | None = None,
        forensic_status: ForensicStatus = ForensicStatus.INCONCLUSIVE,
        groups_manipulation: int = 0,
        on_call: Callable[[str], None] | None = None,
    ) -> None:
        self.label = label
        self.worthiness = worthiness
        self.claims = claims
        self.candidate = candidate or label
        self.delay = delay
        self.fail = fail or {}
        self.identifiable = identifiable
        self.private = private
        self.forensic_status = forensic_status
        self.groups_manipulation = groups_manipulation
        self.calls: list[str] = []
        self.windows: dict[str, tuple[float, float]] = {}
        self.on_call = on_call

    async def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.on_call:
            self.on_call(name)
        loop = asyncio.get_running_loop()
        start = loop.time()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.windows[name] = (start, loop.time())
        if name in self.fail:
            raise self.fail[name]

    async def resolve_input(
        self, request: Any, upload_path: str | None, job_id: str, deps: Any
    ) -> ResolvedInput:
        await self._enter("resolve_input")
        return ResolvedInput(
            input_kind=InputKind.TEXT if request.text and not request.url else InputKind.URL,
            platform=Platform.X if request.url else Platform.TEXT,
            original_url=request.url,
            normalized_url=request.url,
            caption_text=request.text,
            media=[
                MediaAsset(
                    id="M1",
                    kind=MediaKind.IMAGE,
                    path="/tmp/x.jpg",
                    sha256="cd" * 32,
                    phash="0f0f0f0f0f0f0f0f",
                )
            ]
            if request.url
            else [],
            media_accessible=bool(request.url),
            labels=["Visual forensics unavailable — analyzed caption only"] if not request.url else [],
        )

    async def extract_claims(self, resolved: ResolvedInput, locale: str, deps: Any) -> ClaimExtraction:
        await self._enter("extract_claims")
        return extraction(
            text=resolved.caption_text or "claim",
            worthiness=self.worthiness,
            claims=self.claims,
            identifiable=self.identifiable,
            private=self.private,
            name="Jane Doe" if self.identifiable else None,
        )

    async def gather_evidence(self, extraction_: Any, resolved: Any, locale: str, deps: Any) -> BranchResult:
        await self._enter("gather_evidence")
        return BranchResult(
            branch="evidence",
            status=BranchStatus.OK,
            items=[item("E1", "newschecker.in"), item("E2", "factcrescendo.com"), item("E3", "thequint.com")],
            tools_used={"gnews_rss": "news.google.com rss"},
        )

    async def followup_search(self, query: str, deps: Any, *, start_index: int = 1) -> list[EvidenceItem]:
        return []

    async def run_forensics(self, resolved: Any, extraction_: Any, deps: Any) -> ForensicResult:
        await self._enter("run_forensics")
        return forensic(
            self.forensic_status, 0.92 if self.forensic_status is ForensicStatus.MANIPULATED else 0.5
        )

    async def run_provenance(
        self, job_id: str, resolved: Any, extraction_: Any, deps: Any
    ) -> ProvenanceResult:
        await self._enter("run_provenance")
        return ProvenanceResult(
            branch_status=BranchStatus.OK, items=[item("P1", "boomlive.in", branch="provenance")]
        )

    def fuse(
        self, extraction_: Any, evidence: BranchResult, forensic_: Any, provenance: Any, settings: Any
    ) -> FusionResult:
        self.calls.append("fuse")
        if "fuse" in self.fail:
            raise self.fail["fuse"]
        unverifiable = self.candidate is VerdictLabel.UNVERIFIABLE
        return FusionResult(
            truth_score=-0.8,
            manipulation_score=0.9 if self.label is VerdictLabel.MANIPULATED else 0.1,
            p_true=0.12,
            signals=[SignalScore(name="news_stance", score=-1.0, weight=0.25, evidence_ids=["E1", "E2"])],
            groups_refute=3,
            groups_manipulation=self.groups_manipulation,
            candidate=self.candidate,
            candidate_reason=AbstainReason.SINGLE_SOURCE if unverifiable else None,
            evidence_state=EvidenceState.INSUFFICIENT if unverifiable else EvidenceState.REFUTES,
            weights_version="w-test",
            calibration_version="cal-test",
        )

    def decide(
        self,
        extraction_: ClaimExtraction,
        fusion: FusionResult,
        debate: DebateResult | None,
        forensic_: Any,
        provenance: Any,
        all_items: list[EvidenceItem],
        settings: Any,
        *,
        llm_enabled: bool,
        degraded_tools: list[str],
    ) -> Verdict:
        self.calls.append("decide")
        if "decide" in self.fail:
            raise self.fail["decide"]
        if extraction_.check_worthiness < settings.checkworthiness_threshold or not extraction_.claims:
            return Verdict(
                label=VerdictLabel.NOT_CHECKABLE,
                confidence=0.9,
                evidence_state=EvidenceState.NOT_CHECKABLE,
                abstain_detail="No checkable factual claim.",
            )
        if fusion.candidate is VerdictLabel.UNVERIFIABLE:
            return Verdict(
                label=VerdictLabel.UNVERIFIABLE,
                confidence=0.0,
                abstain_reason=AbstainReason.SINGLE_SOURCE,
                evidence_state=EvidenceState.INSUFFICIENT,
            )
        return Verdict(
            label=self.label,
            confidence=0.88,
            evidence_state=EvidenceState.MANIPULATED
            if self.label is VerdictLabel.MANIPULATED
            else EvidenceState.REFUTES,
            rationale=[
                RationaleSentence(text="Fact-checkers report the footage is old.", evidence_ids=["E1", "E2"])
            ],
            cited_evidence_ids=["E1", "E2"],
            independent_source_count=2,
            degraded_tools=list(degraded_tools),
        )

    async def run_debate(
        self, extraction_: Any, fusion: Any, items: Any, forensic_: Any, provenance: Any, deps: Any
    ) -> DebateResult:
        await self._enter("run_debate")
        return DebateResult(
            judge=JudgeOutput(
                label=self.label,
                provider="fake",
                model_id="fake-model",
                rationale=[RationaleSentence(text="Old footage.", evidence_ids=["E1"])],
            ),
            verifier=VerifierOutput(ok=True, methods=["deterministic"]),
        )

    def service_set(self, *, missing: tuple[str, ...] = ()) -> ServiceSet:
        fns: dict[str, Any] = {
            name: getattr(self, name)
            for name in (
                "resolve_input",
                "extract_claims",
                "gather_evidence",
                "followup_search",
                "run_forensics",
                "run_provenance",
                "fuse",
                "decide",
                "run_debate",
            )
        }
        for name in missing:
            fns[name] = None
        return ServiceSet(**fns)
