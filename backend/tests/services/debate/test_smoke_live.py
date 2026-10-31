"""Live smoke (network + real LLM + local models): demo cases (a) TRUE and (d) AMBIGUOUS through
gather_evidence -> fuse -> run_debate -> decide, with cold tool caches. Prints per-stage latency,
evidence counts, independent groups and the LLM cost read from the `llm_call` log lines.

    cd backend && $PY -m pytest tests/services/debate/test_smoke_live.py -m "network and ml" -s
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
import structlog

from veriloop.contracts import (
    AbstainBand,
    BranchStatus,
    Claim,
    ClaimExtraction,
    Entity,
    ForensicResult,
    ForensicStatus,
    ProvenanceResult,
    VerdictLabel,
)

# startup + model prewarm alone takes 60-75 s on a contended laptop: beyond the 120 s default
pytestmark = [pytest.mark.network, pytest.mark.ml, pytest.mark.timeout(600)]

CASES = {
    "a": (
        "The Union Cabinet approved a 3% hike in Dearness Allowance for central government employees and "
        "pensioners on 28 October 2026.",
        [Entity(name="Union Cabinet", type="org"), Entity(name="Dearness Allowance", type="other")],
        {VerdictLabel.TRUE},
    ),
    "d": (
        "The 8th Pay Commission will recommend a fitment factor of 2.86 for central government employees.",
        [Entity(name="8th Pay Commission", type="org")],
        {VerdictLabel.UNVERIFIABLE},
    ),
}


class CostMeter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, _logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
        if event.get("event") == "llm_call":
            self.calls.append(
                {
                    "task": event.get("task"),
                    "model": event.get("model"),
                    "ms": event.get("latency_ms"),
                    "inr": float(event.get("cost_inr") or 0.0),
                }
            )
        return event

    def since(self, n: int) -> dict[str, Any]:
        new = self.calls[n:]
        return {
            "calls": len(new),
            "inr": round(sum(c["inr"] for c in new), 4),
            "by_task": {c["task"]: c["ms"] for c in new},
        }


def _extraction(text: str, entities: list[Entity]) -> ClaimExtraction:
    return ClaimExtraction(
        language="en",
        claims=[Claim(id="C1", text=text, entities=entities)],
        primary_claim_id="C1",
        check_worthiness=0.9,
        worthiness_reason="smoke",
    )


async def test_live_demo_cases_a_and_d():
    from veriloop.bootstrap import shutdown, startup
    from veriloop.core.cache import ToolCache
    from veriloop.core.config import get_settings
    from veriloop.services.debate import run_debate
    from veriloop.services.evidence import gather_evidence
    from veriloop.services.fusion import decide, fuse

    settings = get_settings()
    deps = await startup(settings)
    meter = CostMeter()
    structlog.configure(processors=[meter, *structlog.get_config()["processors"]])
    summary: dict[str, Any] = {"models": deps.resolved_models}
    try:
        t = time.perf_counter()
        from veriloop.services import debate as debate_pkg
        from veriloop.services import evidence as evidence_pkg

        await evidence_pkg.prewarm(deps)  # what the app runs at startup (ML_PREWARM)
        await debate_pkg.prewarm(deps)
        summary["prewarm_ms"] = int((time.perf_counter() - t) * 1000)
        summary["nli"] = deps.nli.status()
        deps.tools.cache = ToolCache()  # cold tool caches (memory only, empty)
        band = AbstainBand(
            low=settings.forensic_low, high=settings.forensic_high, delta=settings.forensic_delta
        )
        fx = ForensicResult(branch_status=BranchStatus.SKIPPED, status=ForensicStatus.UNAVAILABLE, band=band)
        prov = ProvenanceResult(branch_status=BranchStatus.SKIPPED)
        for name, (text, entities, expected) in CASES.items():
            ext = _extraction(text, entities)
            out: dict[str, Any] = {}
            n0 = len(meter.calls)
            t0 = time.perf_counter()
            evidence = await gather_evidence(ext, None, "en-IN", deps)
            t1 = time.perf_counter()
            fusion = fuse(ext, evidence, fx, prov, settings)
            t2 = time.perf_counter()
            n1 = len(meter.calls)
            debate = await run_debate(ext, fusion, evidence.items, fx, prov, deps)
            t3 = time.perf_counter()
            items = [*evidence.items, *debate.followup_items]
            verdict = decide(
                ext, fusion, debate, fx, prov, items, settings, llm_enabled=True, degraded_tools=[]
            )
            t4 = time.perf_counter()
            groups = sorted({i.publisher_group for i in evidence.items if i.tool != "wikidata"})
            out["latency_ms"] = {
                "evidence": int((t1 - t0) * 1000),
                "fuse": int((t2 - t1) * 1000),
                "debate": int((t3 - t2) * 1000),
                "decide": int((t4 - t3) * 1000),
                "total": int((t4 - t0) * 1000),
            }
            out["evidence_timing_note"] = next((n for n in evidence.notes if n.startswith("timing_ms")), "")
            out["evidence"] = {
                "status": evidence.status.value,
                "items": len(evidence.items),
                "groups": len(groups),
                "errors": [f"{e.tool}:{e.error_type}" for e in evidence.errors],
                "gov_items": [i.id for i in evidence.items if i.is_government_source],
                "fact_checks": [i.id for i in evidence.items if i.is_fact_check],
            }
            out["fusion"] = {
                "T": fusion.truth_score,
                "p_true": fusion.p_true,
                "candidate": fusion.candidate.value,
                "reason": fusion.candidate_reason.value if fusion.candidate_reason else None,
                "groups_support": fusion.groups_support,
                "groups_refute": fusion.groups_refute,
                "state": fusion.evidence_state.value,
                "fast_path": fusion.fast_path,
            }
            out["debate"] = {
                "skipped": debate.skipped,
                "judge": debate.judge.label.value if debate.judge else None,
                "judge_extractive": debate.judge.extractive if debate.judge else None,
                "verifier_ok": debate.verifier.ok if debate.verifier else None,
                "verifier_methods": debate.verifier.methods if debate.verifier else None,
                "failing": debate.verifier.failing_sentences if debate.verifier else None,
                "nli": [c.nli_entailment for c in debate.verifier.checks] if debate.verifier else None,
                "second_judge": debate.second_judge.model_dump() if debate.second_judge else None,
                "followup_items": [i.id for i in debate.followup_items],
                "followup_queries": [
                    q for a in (debate.proponent, debate.opponent) if a for q in a.followup_queries
                ],
            }
            out["verdict"] = {
                "label": verdict.label.value,
                "confidence": verdict.confidence,
                "reason": verdict.abstain_reason.value if verdict.abstain_reason else None,
                "detail": verdict.abstain_detail,
                "state": verdict.evidence_state.value,
                "independent_sources": verdict.independent_source_count,
                "cited": verdict.cited_evidence_ids,
                "rationale": [(s.text, s.evidence_ids) for s in verdict.rationale],
                "labels": verdict.labels,
            }
            out["llm"] = {
                "evidence": {"calls": n1 - n0, "inr": round(sum(c["inr"] for c in meter.calls[n0:n1]), 4)},
                "debate": meter.since(n1),
            }
            summary[name] = out

            # Hard guarantees, independent of how live news coverage drifts:
            by_id = {i.id for i in items}
            assert all(c in by_id for c in verdict.cited_evidence_ids)  # G1: cited ids exist
            assert all(s.evidence_ids and set(s.evidence_ids) <= by_id for s in verdict.rationale)
            if verdict.label in (VerdictLabel.TRUE, VerdictLabel.FALSE, VerdictLabel.MANIPULATED):
                assert verdict.independent_source_count >= 2  # G4
                assert debate.verifier is not None and debate.verifier.ok  # D-13
            summary[name]["expected"] = sorted(v.value for v in expected)
    finally:
        await shutdown(deps)
        print("\nSMOKE " + json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    assert VerdictLabel(summary["d"]["verdict"]["label"]) is VerdictLabel.UNVERIFIABLE  # never TRUE/FALSE
    assert VerdictLabel(summary["a"]["verdict"]["label"]) in CASES["a"][2], summary["a"]["verdict"]
