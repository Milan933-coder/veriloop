"""run_debate: Tool-MAD debate -> judge -> dual-key verifier || second judge (SPEC §5, BUILD_SPEC §5-6).

    proponent || opponent        (effort none; each may run ONE followup_search -> items D1..Dn)
      -> judge                   (effort low; sees all items, both arguments, fusion signals)
      -> verifier || second judge (dual-key grounding D-13 || independent model, agreement recorded)

Fast path (>= 2 independent ClaimReviews agree): no proponent/opponent, judge + verifier only.
LLM disabled: extractive mode, no debate; the rationale is quoted evidence sentences with ids.
If the judge call fails, the same extractive judge is used (labelled) rather than inventing anything.
Never raises to the orchestrator.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

import structlog

from veriloop.contracts import (
    Argument,
    ClaimExtraction,
    DebateResult,
    EvidenceItem,
    ForensicResult,
    FusionResult,
    JudgeOutput,
    ProvenanceResult,
    SecondJudge,
    VerdictLabel,
)
from veriloop.core.errors import LLMDisabled, ToolCallFailed
from veriloop.deps import Deps
from veriloop.services.debate.agents import judge_prompt, run_agent, run_judge
from veriloop.services.debate.table import render_context, split_roles
from veriloop.services.debate.verifier import verify
from veriloop.services.evidence import followup_search
from veriloop.services.fusion import extractive_rationale_for

log = structlog.get_logger(__name__)

FOLLOWUP_BUDGET_S = 3.5  # both follow-up searches run in parallel (each has a 4 s source deadline)
FOLLOWUP_MIN_GROUPS = 2  # a side that already has this many independent groups gets no follow-up
_DECISIVE = (VerdictLabel.TRUE, VerdictLabel.FALSE, VerdictLabel.MANIPULATED)


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def extractive_judge(
    fusion: FusionResult,
    items: Sequence[EvidenceItem],
    forensic: ForensicResult | None,
    provenance: ProvenanceResult | None,
    *,
    provider: str = "none",
) -> JudgeOutput:
    """Label = fusion candidate (abstain if not decisive); rationale = quoted evidence with ids."""
    label = fusion.candidate if fusion.candidate in _DECISIVE else VerdictLabel.UNVERIFIABLE
    rationale = extractive_rationale_for(label, list(items), forensic, provenance)
    return JudgeOutput(label=label, rationale=rationale, provider=provider, model_id="none", extractive=True)


def _renumber_followups(
    found: list[list[EvidenceItem]], existing: Sequence[EvidenceItem]
) -> tuple[list[EvidenceItem], list[list[str]]]:
    """Contiguous D ids after any D items already in the job; drop URLs the job already cites."""
    seen_urls = {i.url for i in existing}
    start = 1 + max(
        (int(i.id[1:]) for i in existing if i.id.startswith("D") and i.id[1:].isdigit()), default=0
    )
    out: list[EvidenceItem] = []
    per_agent: list[list[str]] = []
    for batch in found:
        ids: list[str] = []
        for it in batch:
            if it.url in seen_urls:
                continue
            seen_urls.add(it.url)
            new = it.model_copy(update={"id": f"D{start + len(out)}"})
            out.append(new)
            ids.append(new.id)
        per_agent.append(ids)
    return out, per_agent


async def _followups(
    deps: Deps, queries: list[str | None], existing: Sequence[EvidenceItem]
) -> tuple[list[EvidenceItem], list[list[str]]]:
    async def one(q: str | None) -> list[EvidenceItem]:
        return await followup_search(q, deps) if q else []

    try:
        found = await asyncio.wait_for(asyncio.gather(*(one(q) for q in queries)), FOLLOWUP_BUDGET_S)
    except TimeoutError:
        log.warning("debate_followup_timeout", budget_s=FOLLOWUP_BUDGET_S)
        found = [[] for _ in queries]
    return _renumber_followups(list(found), existing)


async def _second_judge(deps: Deps, user: str, known: set[str], primary: JudgeOutput) -> SecondJudge | None:
    llm2 = deps.llm2
    if llm2 is None or not llm2.enabled or not deps.settings.llm_second_judge:
        return None
    try:
        out = await run_judge(llm2, user, known, task="second_judge")
    except (LLMDisabled, ToolCallFailed) as exc:
        msg = exc.error.message if isinstance(exc, ToolCallFailed) else "LLM disabled"
        return SecondJudge(provider=llm2.provider, model_id=llm2.model_id, error=msg[:300])
    return SecondJudge(
        provider=llm2.provider, model_id=llm2.model_id, label=out.label, agrees=out.label is primary.label
    )


async def _extractive_result(
    fusion: FusionResult,
    items: list[EvidenceItem],
    forensic: ForensicResult | None,
    provenance: ProvenanceResult | None,
    deps: Deps,
    t0: float,
    reason: str,
) -> DebateResult:
    judge = extractive_judge(fusion, items, forensic, provenance)
    verifier = await verify(judge, items, deps)
    log.info(
        "debate_done", mode="extractive", reason=reason, label=judge.label.value, verifier_ok=verifier.ok
    )
    return DebateResult(skipped=True, skip_reason=reason, judge=judge, verifier=verifier, latency_ms=_ms(t0))


async def run_debate(
    extraction: ClaimExtraction,
    fusion: FusionResult,
    items: list[EvidenceItem],
    forensic: ForensicResult | None,
    provenance: ProvenanceResult | None,
    deps: Deps,
) -> DebateResult:
    """BUILD_SPEC §5. Never raises: a crash degrades to the extractive judge."""
    t0 = time.perf_counter()
    items = list(items)
    try:
        return await _run(extraction, fusion, items, forensic, provenance, deps, t0)
    except Exception as exc:
        log.exception("debate_failed")
        try:
            return await _extractive_result(
                fusion,
                items,
                forensic,
                provenance,
                deps,
                t0,
                f"debate failed ({type(exc).__name__}); extractive",
            )
        except Exception:
            return DebateResult(
                skipped=True, skip_reason=f"debate failed: {type(exc).__name__}", latency_ms=_ms(t0)
            )


async def _run(
    extraction: ClaimExtraction,
    fusion: FusionResult,
    items: list[EvidenceItem],
    forensic: ForensicResult | None,
    provenance: ProvenanceResult | None,
    deps: Deps,
    t0: float,
) -> DebateResult:
    if extraction is None or extraction.primary_claim() is None:
        return DebateResult(skipped=True, skip_reason="no claim to debate", latency_ms=_ms(t0))
    if not deps.llm.enabled:
        return await _extractive_result(
            fusion, items, forensic, provenance, deps, t0, "LLM disabled — extractive mode (no debate)"
        )
    timings: dict[str, int] = {}
    proponent: Argument | None = None
    opponent: Argument | None = None
    followup_items: list[EvidenceItem] = []

    if not fusion.fast_path:
        t = time.perf_counter()
        pro_items, con_items = split_roles(items)
        context = render_context(forensic, provenance)
        results = await asyncio.gather(
            run_agent(deps.llm, "proponent", extraction, pro_items, context),
            run_agent(deps.llm, "opponent", extraction, con_items, context),
            return_exceptions=True,
        )
        timings["agents"] = _ms(t)
        args: list[Argument | None] = []
        queries: list[str | None] = []
        for role, res in zip(("proponent", "opponent"), results, strict=True):
            if isinstance(res, BaseException):
                err = res.error.message if isinstance(res, ToolCallFailed) else f"{type(res).__name__}: {res}"
                log.warning("debate_agent_failed", role=role, error=str(err)[:200])
                args.append(None)
                queries.append(None)
            else:
                args.append(res[0])
                queries.append(res[1])
        # ONE follow-up per agent, granted only when its side is thin: a side that already has >= 2
        # independent groups gains nothing decisive from 5 more headlines, and every follow-up adds a
        # search round trip before the judge can start (measured 5 s in live runs).
        side_groups = (fusion.groups_support, max(fusion.groups_refute, fusion.groups_manipulation))
        granted = [
            q if q and g < FOLLOWUP_MIN_GROUPS else None for q, g in zip(queries, side_groups, strict=True)
        ]
        if granted != queries:
            log.info(
                "debate_followup_skipped",
                requested=sum(1 for q in queries if q),
                granted=sum(1 for q in granted if q),
            )
        queries = granted
        if any(queries):
            t = time.perf_counter()
            followup_items, per_agent = await _followups(deps, queries, items)
            timings["followup"] = _ms(t)
            for arg, query, ids in zip(args, queries, per_agent, strict=True):
                if arg is not None and query:
                    arg.followup_queries = [query]
                    arg.followup_evidence_ids = ids
        proponent, opponent = args

    all_items = items + followup_items
    known = {i.id for i in all_items}
    user = judge_prompt(
        extraction, fusion, all_items, proponent, opponent, forensic, provenance, fast_path=fusion.fast_path
    )
    t = time.perf_counter()
    try:
        judge = await run_judge(deps.llm, user, known)
    except (LLMDisabled, ToolCallFailed) as exc:
        msg = exc.error.message if isinstance(exc, ToolCallFailed) else "LLM disabled"
        log.warning("judge_failed_extractive_fallback", error=msg[:200])
        judge = extractive_judge(fusion, all_items, forensic, provenance, provider="extractive-fallback")
    timings["judge"] = _ms(t)

    t = time.perf_counter()
    verifier, second = await asyncio.gather(
        verify(judge, all_items, deps), _second_judge(deps, user, known, judge)
    )
    timings["verify"] = _ms(t)
    latency = _ms(t0)
    log.info(
        "debate_done",
        mode="fast_path" if fusion.fast_path else "debate",
        label=judge.label.value,
        extractive=judge.extractive,
        verifier_ok=verifier.ok,
        failing=len(verifier.failing_sentences),
        second_judge=second.label.value if second and second.label else (second.error if second else None),
        second_agrees=second.agrees if second else None,
        followup_items=len(followup_items),
        latency_ms=latency,
        **{f"ms_{k}": v for k, v in timings.items()},
    )
    return DebateResult(
        skipped=False,
        skip_reason="fast path: judge + verifier only" if fusion.fast_path else None,
        proponent=proponent,
        opponent=opponent,
        judge=judge,
        verifier=verifier,
        second_judge=second,
        followup_items=followup_items,
        latency_ms=latency,
    )
