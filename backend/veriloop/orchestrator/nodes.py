"""Graph nodes (BUILD_SPEC §6). Each returns a partial JobState update.

Special keys a node may return (popped by `traced`, never stored in state):
  `_detail`  short human-readable note for the trace / SSE event ("skipped: ..." for skips)
  `_status`  "ok" (default) | "skipped" | "error"

Failure policy (E6): `traced` catches any exception -> node status `error` + ToolError(tool="node:<name>").
Downstream nodes skip, `decide_action` turns any node error into UNVERIFIABLE/TOOL_OUTAGE, and
`audit_write` still records the run. Report/legal actions are never executed here (G5): decide_action
only computes eligibility and build_report only summarises bundle metadata.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import structlog

from veriloop.contracts import (
    AbstainBand,
    AbstainReason,
    AuditEventType,
    BranchResult,
    BranchStatus,
    EvidenceItem,
    EvidenceState,
    ForensicResult,
    ForensicStatus,
    FusionResult,
    JobView,
    NodeTiming,
    ProvenanceResult,
    ToolError,
    Verdict,
    VerdictLabel,
    utcnow,
)
from veriloop.core.logging import redact_text
from veriloop.deps import Deps
from veriloop.services.audit import append_event, build_verdict_payload
from veriloop.services.legal.eligibility import legal_eligibility_parts, report_eligibility_parts

from .jobview import all_items, collect_errors, degraded_tools, node_errors
from .services import ServiceSet

log = structlog.get_logger(__name__)

NodeFn = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]
BRANCHES = ("evidence_branch", "forensic_branch", "provenance_branch")
PIPELINE = (
    "resolve_input",
    "extract_claim",
    "check_worthiness",
    *BRANCHES,
    "fuse_evidence",
    "judge",
    "decide_action",
    "build_report",
    "audit_write",
)
BEFORE_DECIDE = PIPELINE[: PIPELINE.index("decide_action")]
DECISIVE = frozenset({VerdictLabel.TRUE, VerdictLabel.FALSE, VerdictLabel.MANIPULATED})
ViewBuilder = Callable[[Mapping[str, Any]], JobView]


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def publish(deps: Deps, job_id: str, event: dict[str, Any]) -> None:
    try:
        deps.events.publish(job_id, event)
    except Exception as exc:  # progress events are best-effort; never fail a node over them
        log.warning("event_publish_failed", job_id=job_id, error=str(exc))


def traced(name: str, fn: NodeFn, deps: Deps) -> NodeFn:
    """Wrap a node: NodeTiming into state.trace, SSE start/end/error events, logs, error capture."""

    async def node(state: Mapping[str, Any]) -> dict[str, Any]:
        job_id = str(state.get("job_id", "?"))
        started = utcnow()
        t0 = time.perf_counter()
        publish(deps, job_id, {"type": "node", "node": name, "status": "start"})
        log.info("node_start", job_id=job_id, node=name)
        try:
            update = dict(await fn(state) or {})
        except Exception as exc:
            ms = _ms(t0)
            message = redact_text(f"{type(exc).__name__}: {exc}")[:500]
            timing = NodeTiming(
                node=name,
                started_at=started,
                ended_at=utcnow(),
                duration_ms=ms,
                status="error",
                detail=message,
            )
            error = ToolError(tool=f"node:{name}", op=name, error_type="exception", message=message)
            publish(
                deps,
                job_id,
                {"type": "node", "node": name, "status": "error", "duration_ms": ms, "detail": message},
            )
            log.warning("node_failed", job_id=job_id, node=name, duration_ms=ms, error=message)
            return {"trace": [timing], "tool_errors": [error]}
        ms = _ms(t0)
        detail = update.pop("_detail", None)
        status = update.pop("_status", "ok")
        timing = NodeTiming(
            node=name, started_at=started, ended_at=utcnow(), duration_ms=ms, status=status, detail=detail
        )
        event: dict[str, Any] = {
            "type": "node",
            "node": name,
            "status": "error" if status == "error" else "end",
            "duration_ms": ms,
        }
        if detail:
            event["detail"] = detail
        publish(deps, job_id, event)
        log.info("node_end", job_id=job_id, node=name, duration_ms=ms, status=status, detail=detail)
        update["trace"] = [*update.get("trace", []), timing]
        return update

    node.__name__ = name
    return node


# ---- placeholders for branches that produced nothing (node crashed / skipped upstream) ----------


def failed_evidence(reason: str) -> BranchResult:
    return BranchResult(branch="evidence", status=BranchStatus.FAILED, notes=[reason])


def failed_forensic(deps: Deps, reason: str, status: BranchStatus = BranchStatus.FAILED) -> ForensicResult:
    s = deps.settings
    return ForensicResult(
        branch_status=status,
        status=ForensicStatus.UNAVAILABLE,
        band=AbstainBand(low=s.forensic_low, high=s.forensic_high, delta=s.forensic_delta),
        notes=[reason],
    )


def failed_provenance(reason: str, status: BranchStatus = BranchStatus.FAILED) -> ProvenanceResult:
    return ProvenanceResult(branch_status=status, notes=[reason])


def placeholder_fusion(candidate: VerdictLabel, state: EvidenceState) -> FusionResult:
    return FusionResult(
        truth_score=0.0, manipulation_score=0.0, p_true=0.5, candidate=candidate, evidence_state=state
    )


# ---- verdict helpers ----------------------------------------------------------------------------


def outage_verdict(errors: list[ToolError], degraded: list[str]) -> Verdict:
    failed = ", ".join(dict.fromkeys(e.tool.removeprefix("node:") for e in errors)) or "unknown stage"
    first = errors[0].message if errors else ""
    return Verdict(
        label=VerdictLabel.UNVERIFIABLE,
        confidence=0.0,
        abstain_reason=AbstainReason.TOOL_OUTAGE,
        abstain_detail=(
            f"Pipeline stage(s) failed: {failed} ({first}). VeriLoop withholds a verdict instead of guessing "
            "(E6); no evidence was invented."
        )[:1000],
        evidence_state=EvidenceState.INSUFFICIENT,
        degraded_tools=degraded,
    )


def not_checkable_verdict(worthiness: float, threshold: float, has_claims: bool) -> Verdict:
    detail = (
        f"No checkable factual claim was found (check-worthiness {worthiness:.2f} < {threshold:.2f})."
        if has_claims
        else "No factual claim was found in the text, speech or on-screen text (E3)."
    )
    return Verdict(
        label=VerdictLabel.NOT_CHECKABLE,
        confidence=round(max(0.0, min(1.0, 1.0 - worthiness)), 6),
        abstain_detail=detail,
        evidence_state=EvidenceState.NOT_CHECKABLE,
    )


def enforce_citation_gate(verdict: Verdict, items: list[EvidenceItem]) -> Verdict:
    """Defense in depth for G1/G4/D5: a decisive label needs >= 2 independent cited groups that exist."""
    if verdict.label not in DECISIVE:
        return verdict
    by_id = {it.id: it for it in items}
    cited = list(dict.fromkeys(verdict.cited_evidence_ids))
    unknown = [c for c in cited if c not in by_id]
    groups = {by_id[c].publisher_group for c in cited if c in by_id}
    if not unknown and len(groups) >= 2:
        return verdict
    detail = (
        f"Citation gate (G1/G4): a {verdict.label.value} verdict needs at least 2 independent cited "
        "sources from "
        f"this job; found {len(groups)} group(s)"
        + (f" and unknown evidence ids {unknown}" if unknown else "")
        + "."
    )
    log.warning("citation_gate_downgrade", label=verdict.label.value, groups=len(groups), unknown=unknown)
    return verdict.model_copy(
        update={
            "label": VerdictLabel.UNVERIFIABLE,
            "confidence": 0.0,
            "abstain_reason": AbstainReason.SINGLE_SOURCE if groups else AbstainReason.NO_EVIDENCE,
            "abstain_detail": detail,
            "evidence_state": EvidenceState.INSUFFICIENT if groups else EvidenceState.NO_EVIDENCE,
            "cited_evidence_ids": [c for c in cited if c in by_id],
            "labels": [*verdict.labels, "Citation gate downgraded the verdict to UNVERIFIABLE"],
        }
    )


def apply_eligibility(verdict: Verdict, state: Mapping[str, Any]) -> Verdict:
    """Report: FALSE or MANIPULATED. Legal: rule 8 + G9 (computed here, never executed; G5)."""
    extraction = state.get("extraction")
    forensic = state.get("forensic")
    fusion = state.get("fusion")
    report_ok, _ = report_eligibility_parts(verdict.label)
    legal_ok, block = legal_eligibility_parts(
        verdict.label,
        extraction.subject if extraction else None,
        forensic.status if forensic else None,
        fusion.groups_manipulation if fusion else 0,
    )
    return verdict.model_copy(
        update={"report_eligible": report_ok, "legal_eligible": legal_ok, "legal_block_reason": block}
    )


class PipelineNodes:
    """Node implementations bound to one Deps + ServiceSet."""

    def __init__(self, deps: Deps, services: ServiceSet, view_builder: ViewBuilder) -> None:
        self.deps = deps
        self.svc = services
        self.view_builder = view_builder

    @property
    def threshold(self) -> float:
        return self.deps.settings.checkworthiness_threshold

    def functions(self) -> dict[str, NodeFn]:
        return {
            "resolve_input": self.resolve_input,
            "extract_claim": self.extract_claim,
            "check_worthiness": self.check_worthiness,
            "evidence_branch": self.evidence_branch,
            "forensic_branch": self.forensic_branch,
            "provenance_branch": self.provenance_branch,
            "fuse_evidence": self.fuse_evidence,
            "judge": self.judge,
            "decide_action": self.decide_action,
            "build_report": self.build_report,
            "audit_write": self.audit_write,
        }

    # ---- routing -----------------------------------------------------------------------------
    def is_shortcut(self, state: Mapping[str, Any]) -> bool:
        ext = state.get("extraction")
        return ext is not None and (not ext.claims or ext.check_worthiness < self.threshold)

    def route_after_worthiness(self, state: Mapping[str, Any]) -> str | list[str]:
        if node_errors(state) or state.get("extraction") is None or self.is_shortcut(state):
            return "decide_action"
        return list(BRANCHES)

    def route_after_fusion(self, state: Mapping[str, Any]) -> str:
        fusion = state.get("fusion")
        if node_errors(state) or fusion is None:
            return "decide_action"
        if fusion.candidate in (VerdictLabel.UNVERIFIABLE, VerdictLabel.NOT_CHECKABLE):
            return "decide_action"  # cheap abstain: no debate
        return "judge"

    # ---- nodes -------------------------------------------------------------------------------
    async def resolve_input(self, state: Mapping[str, Any]) -> dict[str, Any]:
        resolved = await self.svc.get("resolve_input")(
            state["request"], state.get("upload_path"), state["job_id"], self.deps
        )
        detail = f"{resolved.platform.value}/{resolved.input_kind.value}; media {len(resolved.media)}"
        return {"resolved": resolved, "_detail": detail}

    async def extract_claim(self, state: Mapping[str, Any]) -> dict[str, Any]:
        resolved = state.get("resolved")
        if resolved is None:
            return {"_status": "skipped", "_detail": "skipped: no resolved input (upstream stage failed)"}
        extraction = await self.svc.get("extract_claims")(resolved, state["request"].locale, self.deps)
        return {
            "extraction": extraction,
            "_detail": f"{len(extraction.claims)} claim(s), lang {extraction.language}",
        }

    async def check_worthiness(self, state: Mapping[str, Any]) -> dict[str, Any]:
        ext = state.get("extraction")
        if ext is None:
            return {"_status": "skipped", "_detail": "skipped: no claim extraction (upstream stage failed)"}
        verdict = "pass" if not self.is_shortcut(state) else "NOT_CHECKABLE shortcut"
        return {
            "_detail": f"worthiness {ext.check_worthiness:.2f} vs {self.threshold:.2f}; "
            f"{len(ext.claims)} claim(s) -> {verdict}"
        }

    async def evidence_branch(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.svc.get("gather_evidence")(
            state["extraction"], state["resolved"], state["request"].locale, self.deps
        )
        out: dict[str, Any] = {
            "evidence": result,
            "_detail": f"{result.status.value}; {len(result.items)} item(s), "
            f"{len(result.errors)} tool error(s)",
        }
        if result.status is BranchStatus.SKIPPED:
            out.update(_status="skipped", _detail="skipped: " + ("; ".join(result.notes) or "not applicable"))
        return out

    async def forensic_branch(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.svc.get("run_forensics")(state["resolved"], state["extraction"], self.deps)
        out: dict[str, Any] = {
            "forensic": result,
            "_detail": f"{result.branch_status.value}; {result.status.value}"
            + (f" P(fake)={result.ensemble_score:.2f}" if result.ensemble_score is not None else ""),
        }
        if result.branch_status is BranchStatus.SKIPPED:
            out.update(
                _status="skipped", _detail="skipped: " + ("; ".join(result.notes) or "no visual media")
            )
        return out

    async def provenance_branch(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.svc.get("run_provenance")(
            state["job_id"], state["resolved"], state["extraction"], self.deps
        )
        out: dict[str, Any] = {
            "provenance": result,
            "_detail": f"{result.branch_status.value}; {len(result.known_fake_matches)} known-fake "
            f"match(es), {len(result.appearances)} appearance(s)",
        }
        if result.branch_status is BranchStatus.SKIPPED:
            out.update(_status="skipped", _detail="skipped: " + ("; ".join(result.notes) or "no media"))
        return out

    async def fuse_evidence(self, state: Mapping[str, Any]) -> dict[str, Any]:
        update: dict[str, Any] = {}
        evidence = state.get("evidence")
        if evidence is None:
            evidence = update["evidence"] = failed_evidence("evidence branch produced no result")
        forensic = state.get("forensic")
        if forensic is None:
            forensic = update["forensic"] = failed_forensic(self.deps, "forensic branch produced no result")
        provenance = state.get("provenance")
        if provenance is None:
            provenance = update["provenance"] = failed_provenance("provenance branch produced no result")
        fusion = self.svc.get("fuse")(state["extraction"], evidence, forensic, provenance, self.deps.settings)
        update["fusion"] = fusion
        update["_detail"] = (
            f"T={fusion.truth_score:+.2f} M={fusion.manipulation_score:.2f} p_true={fusion.p_true:.2f} "
            f"-> candidate {fusion.candidate.value}"
        )
        return update

    async def judge(self, state: Mapping[str, Any]) -> dict[str, Any]:
        debate = await self.svc.get("run_debate")(
            state["extraction"],
            state["fusion"],
            all_items(state),
            state.get("forensic"),
            state.get("provenance"),
            self.deps,
        )
        judge_label = debate.judge.label.value if debate.judge else "none"
        verifier = "ok" if debate.verifier and debate.verifier.ok else "failed" if debate.verifier else "n/a"
        return {"debate": debate, "_detail": f"judge {judge_label}; verifier {verifier}"}

    def _skipped_timings(self, state: Mapping[str, Any], reason: str) -> list[NodeTiming]:
        """Trace + SSE entries for nodes the graph never ran, so the timeline is complete."""
        ran = {t.node for t in state.get("trace") or []}
        now = utcnow()
        out: list[NodeTiming] = []
        for name in BEFORE_DECIDE:
            if name in ran:
                continue
            detail = f"skipped: {reason}"
            out.append(
                NodeTiming(
                    node=name, started_at=now, ended_at=now, duration_ms=0, status="skipped", detail=detail
                )
            )
            publish(
                self.deps,
                state["job_id"],
                {"type": "node", "node": name, "status": "end", "duration_ms": 0, "detail": detail},
            )
        return out

    async def decide_action(self, state: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.deps.settings
        errors = node_errors(state)
        ext = state.get("extraction")
        items = all_items(state)
        degraded = degraded_tools(collect_errors(state))
        extra_errors: list[ToolError] = []

        if errors or ext is None:
            reason = "upstream stage failed"
        elif self.is_shortcut(state):
            reason = (
                f"not check-worthy (worthiness {ext.check_worthiness:.2f} < {self.threshold:.2f})"
                if ext.claims
                else "no checkable claim"
            )
        else:
            reason = "fusion candidate UNVERIFIABLE (cheap abstain, no debate)"
        skipped = self._skipped_timings(state, reason)

        if errors or ext is None:
            verdict = outage_verdict(
                errors
                or [
                    ToolError(
                        tool="node:extract_claim",
                        op="extract_claim",
                        error_type="exception",
                        message="no claim extraction",
                    )
                ],
                degraded,
            )
        elif self.is_shortcut(state):
            verdict = self._not_checkable(state)
        else:
            try:
                verdict = self.svc.get("decide")(
                    ext,
                    state.get("fusion")
                    or placeholder_fusion(VerdictLabel.UNVERIFIABLE, EvidenceState.INSUFFICIENT),
                    state.get("debate"),
                    state.get("forensic") or failed_forensic(self.deps, "missing"),
                    state.get("provenance") or failed_provenance("missing"),
                    items,
                    settings,
                    llm_enabled=self.deps.llm.enabled,
                    degraded_tools=degraded,
                )
            except Exception as exc:
                message = redact_text(f"{type(exc).__name__}: {exc}")[:500]
                log.warning("decide_failed", job_id=state["job_id"], error=message)
                err = ToolError(
                    tool="node:decide_action", op="decide", error_type="exception", message=message
                )
                extra_errors.append(err)
                verdict = outage_verdict([err], degraded)
        verdict = apply_eligibility(enforce_citation_gate(verdict, items), state)
        out: dict[str, Any] = {
            "verdict": verdict,
            "trace": skipped,
            "_detail": f"{verdict.label.value} conf {verdict.confidence:.2f}"
            + (f" ({verdict.abstain_reason.value})" if verdict.abstain_reason else ""),
        }
        if extra_errors:
            out["tool_errors"] = extra_errors
            out["_status"] = "error"
        return out

    def _not_checkable(self, state: Mapping[str, Any]) -> Verdict:
        """E3 shortcut: prefer the fusion package's rule 1, fall back to the same rule locally."""
        ext = state["extraction"]
        try:
            verdict = self.svc.get("decide")(
                ext,
                placeholder_fusion(VerdictLabel.NOT_CHECKABLE, EvidenceState.NOT_CHECKABLE),
                None,
                failed_forensic(self.deps, "not run: claim not check-worthy", BranchStatus.SKIPPED),
                failed_provenance("not run: claim not check-worthy", BranchStatus.SKIPPED),
                [],
                self.deps.settings,
                llm_enabled=self.deps.llm.enabled,
                degraded_tools=[],
            )
            if verdict.label is VerdictLabel.NOT_CHECKABLE:
                return verdict
            log.warning("decide_disagrees_with_shortcut", label=verdict.label.value)
        except Exception as exc:
            log.info("decide_unavailable_for_shortcut", error=str(exc)[:300])
        return not_checkable_verdict(ext.check_worthiness, self.threshold, bool(ext.claims))

    async def build_report(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Eligibility + bundle metadata only. Nothing is prepared or sent here (G5 hard stop)."""
        verdict = state.get("verdict")
        if verdict is None:
            return {"_status": "skipped", "_detail": "skipped: no verdict"}
        resolved = state.get("resolved")
        media = len(resolved.media) if resolved else 0
        detail = (
            f"report {'eligible' if verdict.report_eligible else 'not eligible'}, legal "
            f"{'eligible' if verdict.legal_eligible else 'not eligible'}; bundle: "
            f"{len(verdict.cited_evidence_ids)} cited source(s), {media} media hash(es); "
            "awaiting human confirmation"
        )
        return {"_detail": detail}

    async def audit_write(self, state: Mapping[str, Any]) -> dict[str, Any]:
        view = self.view_builder(state)
        payload = build_verdict_payload(view, state.get("request"), self.deps.settings)
        last: Exception | None = None
        for attempt in (1, 2):  # one retry: a transient DB hiccup must not leave a run unaudited
            try:
                entry = await append_event(self.deps, state["job_id"], AuditEventType.VERDICT, payload)
                return {
                    "audit_id": entry.id,
                    "_detail": f"entry #{entry.id} sha256 {entry.payload_hash[:12]}...",
                }
            except Exception as exc:
                last = exc
                log.warning(
                    "audit_write_failed", job_id=state["job_id"], attempt=attempt, error=str(exc)[:300]
                )
        message = redact_text(f"{type(last).__name__}: {last}")[:500]
        return {
            "tool_errors": [
                ToolError(
                    tool="node:audit_write",
                    op="append_event",
                    error_type="exception",
                    message=message,
                    attempts=2,
                )
            ],
            "_status": "error",
            "_detail": message,
        }
