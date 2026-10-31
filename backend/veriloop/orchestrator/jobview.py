"""Assemble the public `JobView` (GET /verify/{id}) from graph state + job row metadata.

Every JobView field is always populated (the frontend reads all of them): lists default to [], and
the view is serialised with defaults included. `sources` = cited items first (in citation order),
then every other item from the evidence, provenance and debate follow-up branches.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from veriloop.contracts import (
    EvidenceItem,
    InputKind,
    InputSummary,
    JobStatus,
    JobView,
    Platform,
    ResolvedInput,
    ToolError,
    Verdict,
    VerifyRequest,
)
from veriloop.core.config import Settings
from veriloop.core.tools_registry import TOOL_NAMES

# Failures that do not mean "this tool is degraded": a keyed tool without its key is expected (D-15).
_NOT_DEGRADED = frozenset({"unconfigured"})


def all_items(state: Mapping[str, Any]) -> list[EvidenceItem]:
    """Every citable item of the job, deduplicated by id (first occurrence wins)."""
    seen: set[str] = set()
    out: list[EvidenceItem] = []
    groups: list[Iterable[EvidenceItem]] = []
    if (ev := state.get("evidence")) is not None:
        groups.append(ev.items)
    if (prov := state.get("provenance")) is not None:
        groups.append(prov.items)
    if (debate := state.get("debate")) is not None:
        groups.append(debate.followup_items)
    for group in groups:
        for it in group:
            if it.id not in seen:
                seen.add(it.id)
                out.append(it)
    return out


def ordered_sources(items: list[EvidenceItem], cited: list[str]) -> list[EvidenceItem]:
    by_id = {it.id: it for it in items}
    first = [by_id[i] for i in dict.fromkeys(cited) if i in by_id]
    rest = [it for it in items if it.id not in set(cited)]
    return first + rest


def collect_errors(state: Mapping[str, Any]) -> list[ToolError]:
    """Node errors + every branch's logged tool errors (G3), deduplicated."""
    errors: list[ToolError] = list(state.get("tool_errors") or [])
    for key in ("evidence", "forensic", "provenance"):
        branch = state.get(key)
        if branch is not None:
            errors.extend(branch.errors)
    seen: set[tuple[str, str, str, str]] = set()
    out: list[ToolError] = []
    for err in errors:
        sig = (err.tool, err.op, err.error_type, err.message)
        if sig not in seen:
            seen.add(sig)
            out.append(err)
    return out


def node_errors(state: Mapping[str, Any]) -> list[ToolError]:
    return [e for e in state.get("tool_errors") or [] if e.tool.startswith("node:")]


def degraded_tools(errors: Iterable[ToolError], verdict: Verdict | None = None) -> list[str]:
    """Registry tool names that failed in this job (real failures, chaos, open breakers)."""
    names = [e.tool for e in errors if e.tool in TOOL_NAMES and e.error_type not in _NOT_DEGRADED]
    if verdict is not None:
        names.extend(t for t in verdict.degraded_tools if t in TOOL_NAMES)
    return sorted(set(names))


def input_summary(resolved: ResolvedInput | None, request: VerifyRequest | None) -> InputSummary | None:
    if resolved is not None:
        return InputSummary(
            platform=resolved.platform,
            input_kind=resolved.input_kind.value,
            url=resolved.normalized_url or resolved.original_url,
            title=resolved.title,
            media_accessible=resolved.media_accessible,
            media_count=len(resolved.media),
            sampling=resolved.sampling,
            fetch_methods=list(resolved.fetch_methods),
            labels=list(resolved.labels),
            media_hashes=[
                {
                    "id": m.id,
                    "kind": m.kind.value,
                    "sha256": m.sha256,
                    "phash": m.phash,
                    "frame_ts": m.frame_ts,
                }
                for m in resolved.media
            ],
        )
    if request is None:
        return None
    if request.upload_id:
        kind, platform = InputKind.UPLOAD, Platform.UPLOAD
    elif request.url:
        kind, platform = InputKind.URL, Platform.WEB
    else:
        kind, platform = InputKind.TEXT, Platform.TEXT
    return InputSummary(platform=request.platform or platform, input_kind=kind.value, url=request.url)


def _dedupe(labels: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(label for label in labels if label))


def stage_labels(state: Mapping[str, Any], settings: Settings) -> list[str]:
    """Mode labels (REPLAY MODE, LLM disabled ...) + every stage's user-visible caveats."""
    labels: list[str] = list(settings.mode_labels())
    if (resolved := state.get("resolved")) is not None:
        labels.extend(resolved.labels)
        if resolved.sampling is not None and resolved.sampling.note:
            labels.append(resolved.sampling.note)
    if (
        (forensic := state.get("forensic")) is not None
        and forensic.sampling is not None
        and forensic.sampling.note
    ):
        labels.append(forensic.sampling.note)
    if (verdict := state.get("verdict")) is not None:
        labels.extend(verdict.labels)
    failed = [e.tool.removeprefix("node:") for e in node_errors(state)]
    withheld = [name for name in failed if name != "audit_write"]
    if withheld:
        labels.append("Pipeline stage failed: " + ", ".join(dict.fromkeys(withheld)) + " — verdict withheld")
    if "audit_write" in failed:
        labels.append("Audit record could not be written — see tool errors")
    return _dedupe(labels)


def build_job_view(
    state: Mapping[str, Any],
    *,
    job_id: str,
    status: JobStatus,
    created_at: datetime,
    settings: Settings,
    resolved_models: Mapping[str, str],
    source: str = "web",
    mode: str | None = None,
    updated_at: datetime | None = None,
    queue_position: int | None = None,
    status_detail: str | None = None,
    redacted: bool = False,
    error: str | None = None,
    audit_id: int | None = None,
) -> JobView:
    verdict: Verdict | None = state.get("verdict")
    items = all_items(state)
    cited = list(verdict.cited_evidence_ids) if verdict else []
    errors = collect_errors(state)
    view = JobView(
        job_id=job_id,
        status=status,
        created_at=created_at,
        updated_at=updated_at,
        source=source,  # type: ignore[arg-type]
        mode=mode or settings.tool_mode,
        verdict=verdict.label if verdict else None,
        confidence=verdict.confidence if verdict else None,
        abstain_reason=verdict.abstain_reason if verdict else None,
        abstain_detail=verdict.abstain_detail if verdict else None,
        evidence_state=verdict.evidence_state if verdict else None,
        rationale=list(verdict.rationale) if verdict else [],
        cited_evidence_ids=cited,
        independent_source_count=verdict.independent_source_count if verdict else 0,
        sources=ordered_sources(items, cited),
        forensic=state.get("forensic"),
        provenance=state.get("provenance"),
        claim=state.get("extraction"),
        input=input_summary(state.get("resolved"), state.get("request")),
        fusion=state.get("fusion"),
        debate=state.get("debate"),
        trace=list(state.get("trace") or []),
        tool_errors=errors,
        degraded_tools=degraded_tools(errors, verdict),
        labels=stage_labels(state, settings),
        report_eligible=verdict.report_eligible if verdict else False,
        legal_eligible=verdict.legal_eligible if verdict else False,
        legal_block_reason=verdict.legal_block_reason if verdict else None,
        audit_id=audit_id if audit_id is not None else state.get("audit_id"),
        resolved_models=dict(resolved_models),
        queue_position=queue_position,
        status_detail=status_detail,
        redacted=redacted,
        error=error,
    )
    return view


def redact_view(view: JobView) -> JobView:
    """Strip user content after the Telegram TTL (G6/D7); verdict metadata and tool evidence stay."""
    inp = view.input.model_copy(update={"url": None, "title": None}) if view.input else None
    return view.model_copy(update={"claim": None, "input": inp, "debate": None, "redacted": True})


def view_json(view: JobView) -> dict[str, Any]:
    """Serialise with every field present (defaults included), as the frontend expects."""
    return view.model_dump(mode="json")
