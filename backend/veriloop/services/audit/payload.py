"""The VERDICT audit payload (BUILD_SPEC §8): everything needed to re-judge a run later.

Contents: verdict/label/confidence/abstain reason, evidence (id, url, domain, published_at,
retrieval_method, ...), media hashes, forensic model scores, fusion weights + thresholds +
calibration version, trace summary, mode and labels. `append_event` adds job_id, event_type,
recorded_at, resolved_model_ids and tool_versions itself (G7).

Telegram jobs (D-14/G6): user-supplied content (message text, URL, extracted claim text, post title,
rationale wording) is replaced by its sha256 so the chain never holds content past its TTL.
"""

from __future__ import annotations

from typing import Any

from veriloop.contracts import JobView, VerifyRequest
from veriloop.core.config import Settings
from veriloop.core.hashing import sha256_hex


def _h(value: str | None) -> str | None:
    return sha256_hex(value) if value else None


def is_private_source(view: JobView) -> bool:
    """Jobs whose user content must only be stored as hashes in the audit chain."""
    return view.source == "telegram"


def settings_thresholds(settings: Settings) -> dict[str, float]:
    return {
        "abstain_threshold": settings.abstain_threshold,
        "checkworthiness_threshold": settings.checkworthiness_threshold,
        "manipulation_min": settings.manipulation_min,
        "forensic_low": settings.forensic_low,
        "forensic_high": settings.forensic_high,
        "forensic_delta": settings.forensic_delta,
        "nli_entail_min": settings.nli_entail_min,
    }


def _input_section(view: JobView, request: VerifyRequest | None, private: bool) -> dict[str, Any]:
    inp = view.input
    url = (inp.url if inp else None) or (request.url if request else None)
    text = request.text if request else None
    out: dict[str, Any] = {
        "platform": inp.platform.value
        if inp
        else (request.platform.value if request and request.platform else None),
        "input_kind": inp.input_kind if inp else None,
        "media_accessible": inp.media_accessible if inp else False,
        "media_count": inp.media_count if inp else 0,
        "fetch_methods": list(inp.fetch_methods) if inp else [],
        "sampling": inp.sampling.model_dump(mode="json") if inp and inp.sampling else None,
        "locale": request.locale if request else None,
        "upload": bool(request and request.upload_id),
    }
    if private:
        out.update(url_sha256=_h(url), text_sha256=_h(text), title_sha256=_h(inp.title if inp else None))
    else:
        out.update(url=url, text_sha256=_h(text), title=inp.title if inp else None)
    return out


def _claim_section(view: JobView, private: bool) -> dict[str, Any] | None:
    ext = view.claim
    if ext is None:
        return None
    primary = ext.primary_claim()
    text = primary.text if primary else None
    subject = ext.subject
    out: dict[str, Any] = {
        "claim_id": primary.id if primary else None,
        "language": ext.language,
        "claims": len(ext.claims),
        "check_worthiness": ext.check_worthiness,
        "llm_used": ext.llm_used,
        "subject": {
            "is_identifiable_individual": subject.is_identifiable_individual,
            "is_private_individual": subject.is_private_individual,
        },
    }
    if private:
        out["text_sha256"] = _h(text)
        out["subject"]["name_sha256"] = _h(subject.name)
    else:
        out["text"] = text
        out["text_en"] = primary.text_en if primary else None
        out["subject"]["name"] = subject.name
    return out


def _verdict_section(view: JobView, private: bool) -> dict[str, Any]:
    rationale = [
        {"text_sha256": _h(r.text), "evidence_ids": r.evidence_ids}
        if private
        else {"text": r.text, "evidence_ids": r.evidence_ids}
        for r in view.rationale
    ]
    # abstain_detail can quote the claim or a node error that echoes the user's URL: hash it (D-14).
    detail_key = "abstain_detail_sha256" if private else "abstain_detail"
    return {
        "label": view.verdict.value if view.verdict else None,
        "confidence": view.confidence,
        "abstain_reason": view.abstain_reason.value if view.abstain_reason else None,
        detail_key: _h(view.abstain_detail) if private else view.abstain_detail,
        "evidence_state": view.evidence_state.value if view.evidence_state else None,
        "cited_evidence_ids": view.cited_evidence_ids,
        "independent_source_count": view.independent_source_count,
        "rationale": rationale,
        "report_eligible": view.report_eligible,
        "legal_eligible": view.legal_eligible,
        "legal_block_reason": view.legal_block_reason,
    }


def _evidence_section(view: JobView) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "url": item.url,
            "domain": item.domain,
            "publisher_group": item.publisher_group,
            "source_name": item.source_name,
            "published_at": item.published_at,
            "retrieved_at": item.retrieved_at,
            "retrieval_method": item.retrieval_method.value,
            "tool": item.tool,
            "tool_version": item.tool_version,
            "branch": item.branch,
            "stance": item.stance.value,
            "is_fact_check": item.is_fact_check,
            "is_government_source": item.is_government_source,
            "cited": item.id in view.cited_evidence_ids,
        }
        for item in view.sources
    ]


def _forensic_section(view: JobView) -> dict[str, Any] | None:
    f = view.forensic
    if f is None:
        return None
    return {
        "branch_status": f.branch_status.value,
        "status": f.status.value,
        "ensemble_score": f.ensemble_score,
        "disagreement": f.disagreement,
        "band": f.band.model_dump(mode="json"),
        "frames_analyzed": f.frames_analyzed,
        "faces_found": f.faces_found,
        "models": [
            {
                "model_id": m.model_id,
                "model_version": m.model_version,
                "provider": m.provider,
                "raw_score": m.raw_score,
                "weight": m.weight,
                "frames_scored": m.frames_scored,
                "error": m.error,
            }
            for m in f.models
        ],
        "c2pa_present": f.metadata.c2pa_present if f.metadata else None,
        "tools_used": f.tools_used,
    }


def _provenance_section(view: JobView) -> dict[str, Any] | None:
    p = view.provenance
    if p is None:
        return None
    first = p.first_seen_estimate
    return {
        "branch_status": p.branch_status.value,
        "known_fake_matches": [
            {
                "known_fake_id": m.known_fake_id,
                "label": m.label,
                "hamming": m.hamming,
                "clip_similarity": m.clip_similarity,
            }
            for m in p.known_fake_matches
        ],
        "first_seen": {
            "url": first.url,
            "first_seen_at": first.first_seen_at,
            "source": first.timestamp_source,
        }
        if first
        else None,
        "out_of_context_score": p.out_of_context_score,
        "context_flags": p.context_flags,
        "prior_run_matches": p.prior_run_matches,
    }


def _fusion_section(view: JobView) -> dict[str, Any]:
    f = view.fusion
    if f is None:
        return {
            "fusion": None,
            "weights": {},
            "weights_version": None,
            "calibration_version": None,
            "signals": [],
        }
    return {
        "fusion": {
            "truth_score": f.truth_score,
            "manipulation_score": f.manipulation_score,
            "p_true": f.p_true,
            "candidate": f.candidate.value,
            "candidate_reason": f.candidate_reason.value if f.candidate_reason else None,
            "groups_support": f.groups_support,
            "groups_refute": f.groups_refute,
            "groups_manipulation": f.groups_manipulation,
            "fast_path": f.fast_path,
        },
        "signals": [
            {
                "name": s.name,
                "score": s.score,
                "weight": s.weight,
                "reliability": s.reliability,
                "present": s.present,
            }
            for s in f.signals
        ],
        "weights": {s.name: s.weight for s in f.signals},
        "weights_version": f.weights_version,
        "calibration_version": f.calibration_version,
        "fusion_thresholds": f.thresholds,
    }


def _debate_section(view: JobView) -> dict[str, Any] | None:
    d = view.debate
    if d is None:
        return None
    return {
        "skipped": d.skipped,
        "skip_reason": d.skip_reason,
        "judge": {
            "label": d.judge.label.value,
            "model_id": d.judge.model_id,
            "provider": d.judge.provider,
            "extractive": d.judge.extractive,
        }
        if d.judge
        else None,
        "verifier_ok": d.verifier.ok if d.verifier else None,
        "verifier_methods": d.verifier.methods if d.verifier else [],
        "second_judge": {
            "model_id": d.second_judge.model_id,
            "label": d.second_judge.label.value if d.second_judge.label else None,
            "agrees": d.second_judge.agrees,
            "error": d.second_judge.error,
        }
        if d.second_judge
        else None,
        "followup_item_ids": [i.id for i in d.followup_items],
    }


def build_verdict_payload(view: JobView, request: VerifyRequest | None, settings: Settings) -> dict[str, Any]:
    """Payload for the VERDICT row of `view` (append_event adds the reserved fields)."""
    private = is_private_source(view)
    media_hashes = list(view.input.media_hashes) if view.input else []
    return {
        "mode": view.mode,
        "source": view.source,
        "content_policy": "sha256_only" if private else "full",
        "created_at": view.created_at,
        "input": _input_section(view, request, private),
        "claim": _claim_section(view, private),
        "verdict": _verdict_section(view, private),
        "evidence": _evidence_section(view),
        "media_hashes": [
            {k: h.get(k) for k in ("id", "kind", "sha256", "phash", "frame_ts")} for h in media_hashes
        ],
        "forensic": _forensic_section(view),
        "provenance": _provenance_section(view),
        **_fusion_section(view),
        "thresholds": settings_thresholds(settings),
        "debate": _debate_section(view),
        # Node details can echo input text, so private (Telegram) runs keep only timings.
        "trace": [
            {
                "node": t.node,
                "status": t.status,
                "duration_ms": t.duration_ms,
                "detail": None if private else t.detail,
            }
            for t in view.trace
        ],
        "tool_errors": [
            {"tool": e.tool, "op": e.op, "error_type": e.error_type, "attempts": e.attempts}
            for e in view.tool_errors
        ],
        "degraded_tools": view.degraded_tools,
        **_labels_section(view.labels, settings, private),
    }


def _labels_section(labels: list[str], settings: Settings, private: bool) -> dict[str, Any]:
    """Stage labels can echo input (e.g. "could not fetch <url>"): private runs keep mode labels + hashes."""
    if not private:
        return {"labels": labels}
    fixed = set(settings.mode_labels())
    return {
        "labels": [label for label in labels if label in fixed],
        "labels_sha256": [sha256_hex(label) for label in labels if label not in fixed],
    }
