"""LLM debate agents: proponent / opponent (effort none) and judge (effort low).

Agents only reason over evidence items of this job (D-16). Their outputs are sanitised here:
ids are normalised and any id that does not exist in the job is dropped; a point left without a
valid id is dropped (it could not be grounded). The verifier re-checks the judge independently.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from veriloop.contracts import (
    Argument,
    ClaimExtraction,
    EvidenceItem,
    ForensicResult,
    FusionResult,
    JudgeOutput,
    ProvenanceResult,
    RationaleSentence,
    VerdictLabel,
)
from veriloop.core.llm import LLMClient
from veriloop.services.debate import prompts
from veriloop.services.debate.schemas import AgentOut, JudgeOut, Point
from veriloop.services.debate.table import (
    claim_text,
    normalize_id,
    render_argument,
    render_context,
    render_fusion,
    render_table,
)

log = structlog.get_logger(__name__)

MAX_POINTS = 4
MAX_RATIONALE = 5


def clean_points(points: Sequence[Point], known: set[str], limit: int) -> tuple[list[RationaleSentence], int]:
    """Normalise ids, keep only ids that exist in this job; drop points left without one."""
    out: list[RationaleSentence] = []
    dropped = 0
    for p in points:
        text = " ".join((p.text or "").split())
        ids = [i for i in (normalize_id(r) for r in p.evidence_ids) if i and i in known]
        ids = list(dict.fromkeys(ids))
        if not text or not ids:
            dropped += 1
            continue
        out.append(RationaleSentence(text=text, evidence_ids=ids))
        if len(out) >= limit:
            break
    return out, dropped


async def run_agent(
    llm: LLMClient,
    role: str,
    extraction: ClaimExtraction,
    items: Sequence[EvidenceItem],
    context: str,
) -> tuple[Argument, str | None]:
    """One proponent/opponent turn. Returns the argument and its follow-up query (or None).

    Raises LLMDisabled / ToolCallFailed (the caller degrades)."""
    system = prompts.PROPONENT if role == "proponent" else prompts.OPPONENT
    user = f"CLAIM: {claim_text(extraction)}\n\nYOUR EVIDENCE ITEMS:\n{render_table(items)}" + (
        f"\n\nCONTEXT (not citable):\n{context}" if role == "opponent" else ""
    )
    out = await llm.complete_json(
        task=role, system=system, user=user, schema=AgentOut, effort="none", max_tokens=1200
    )
    known = {i.id for i in items}
    points, dropped = clean_points(out.points, known, MAX_POINTS)
    if dropped:
        log.info("debate_points_dropped", role=role, dropped=dropped)
    query = " ".join((out.followup_query or "").split())[:200] or None
    return Argument(role=role, points=points), query


def judge_prompt(
    extraction: ClaimExtraction,
    fusion: FusionResult,
    items: Sequence[EvidenceItem],
    proponent: Argument | None,
    opponent: Argument | None,
    forensic: ForensicResult | None,
    provenance: ProvenanceResult | None,
    *,
    fast_path: bool,
) -> str:
    parts = [
        f"CLAIM: {claim_text(extraction)}",
        f"EVIDENCE TABLE:\n{render_table(items)}",
        f"FUSION SIGNALS (not citable): {render_fusion(fusion)}",
        f"CONTEXT (not citable):\n{render_context(forensic, provenance)}",
    ]
    if fast_path:
        parts.append("DEBATE: skipped (fast path: independent fact-checks agree at high similarity).")
    else:
        parts.append(
            "DEBATE:\n"
            + render_argument(proponent, "proponent")
            + "\n"
            + render_argument(opponent, "opponent")
        )
    return "\n\n".join(parts)


async def run_judge(
    llm: LLMClient,
    user: str,
    known: set[str],
    *,
    task: str = "judge",
) -> JudgeOutput:
    """Raises LLMDisabled / ToolCallFailed."""
    out = await llm.complete_json(
        task=task, system=prompts.JUDGE, user=user, schema=JudgeOut, effort="low", max_tokens=1500
    )
    # Ids are normalised but NOT filtered here: an invented id must reach the verifier and fail it.
    rationale = []
    for p in out.rationale[:MAX_RATIONALE]:
        text = " ".join((p.text or "").split())
        if not text:
            continue
        ids = list(dict.fromkeys(normalize_id(r) or r.strip() for r in p.evidence_ids if r.strip()))
        rationale.append(RationaleSentence(text=text, evidence_ids=ids))
    unknown = sorted({i for s in rationale for i in s.evidence_ids if i not in known})
    if unknown:
        log.warning("judge_cited_unknown_ids", task=task, ids=unknown)
    return JudgeOutput(
        label=VerdictLabel(out.label),
        rationale=rationale,
        provider=llm.provider,
        model_id=llm.model_id,
        extractive=False,
    )
