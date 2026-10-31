"""Evidence bundle (SPEC §7): verdict.json, sources.json, forensic_report.json, media hashes (SHA-256 +
pHash), timestamps, audit_id and links to the signed PDF / chain verification. Everything is copied
from the JobView (tool outputs only); nothing is synthesized.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from veriloop.contracts import EvidenceBundle, JobView, utcnow
from veriloop.core.config import Settings
from veriloop.core.hashing import normalize_for_json, sha256_hex

from . import links


def _primary_claim(job: JobView) -> str | None:
    if job.claim is None:
        return None
    claim = job.claim.primary_claim()
    return claim.text if claim else job.claim.caption_text


def verdict_doc(job: JobView) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "verdict": job.verdict.value if job.verdict else None,
        "confidence": job.confidence,
        "abstain_reason": job.abstain_reason.value if job.abstain_reason else None,
        "abstain_detail": job.abstain_detail,
        "evidence_state": job.evidence_state.value if job.evidence_state else None,
        "claim": None if job.redacted else _primary_claim(job),
        "claim_redacted": job.redacted,
        "content_url": job.input.url if job.input else None,
        "platform": job.input.platform.value if job.input else None,
        "rationale": [r.model_dump(mode="json") for r in job.rationale],
        "cited_evidence_ids": job.cited_evidence_ids,
        "independent_source_count": job.independent_source_count,
        "mode": job.mode,
        "labels": job.labels,
        "resolved_models": job.resolved_models,
        "audit_id": job.audit_id,
    }


def sources_doc(job: JobView) -> list[dict[str, Any]]:
    cited = set(job.cited_evidence_ids)
    return [{**s.model_dump(mode="json"), "cited": s.id in cited} for s in job.sources]


def forensic_doc(job: JobView) -> dict[str, Any] | None:
    if job.forensic is None and job.provenance is None:
        return None
    return {
        "forensic": job.forensic.model_dump(mode="json") if job.forensic else None,
        "provenance": {
            "branch_status": job.provenance.branch_status.value,
            "known_fake_matches": [m.model_dump(mode="json") for m in job.provenance.known_fake_matches],
            "first_seen_estimate": job.provenance.first_seen_estimate.model_dump(mode="json")
            if job.provenance.first_seen_estimate
            else None,
            "out_of_context_score": job.provenance.out_of_context_score,
            "context_flags": job.provenance.context_flags,
        }
        if job.provenance
        else None,
    }


def build_bundle(job: JobView, settings: Settings) -> EvidenceBundle:
    timestamps = {"job_created_at": job.created_at.isoformat(), "bundle_generated_at": utcnow().isoformat()}
    if job.updated_at is not None:
        timestamps["job_completed_at"] = job.updated_at.isoformat()
    return EvidenceBundle(
        verdict=verdict_doc(job),
        sources=sources_doc(job),
        forensic_report=forensic_doc(job),
        media_hashes=[dict(h) for h in (job.input.media_hashes if job.input else [])],
        timestamps=timestamps,
        audit_id=job.audit_id,
        pdf_url=links.audit_pdf_url(settings, job.job_id),
        verify_url=links.audit_verify_url(settings, job.job_id),
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(normalize_for_json(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def bundle_file_hashes(bundle: EvidenceBundle) -> dict[str, str]:
    """sha256 of each JSON member, recorded in the REPORT_PREPARED audit row."""
    return {
        "verdict.json": sha256_hex(_json_bytes(bundle.verdict)),
        "sources.json": sha256_hex(_json_bytes(bundle.sources)),
        "forensic_report.json": sha256_hex(_json_bytes(bundle.forensic_report)),
        "media_hashes.json": sha256_hex(_json_bytes(bundle.media_hashes)),
    }


README = """VeriLoop evidence bundle
========================

verdict.json          verdict, confidence, abstain reason, rationale with evidence ids
sources.json          every evidence item (url, publisher, dates, retrieval method); cited ones first
forensic_report.json  forensic model scores, metadata/C2PA notes, provenance (known-fake matches)
media_hashes.json     SHA-256 + 64-bit pHash of every analysed image / frame
bundle.json           timestamps, audit id, links, sha256 of each file above
report_card.png       shareable card with a QR code to the audit page
audit_record.pdf      the signed audit record (when the run has been audited)

Verify independently: GET {verify_url}  (recomputes the whole hash chain)
Public key:           GET {pubkey_url}  (Ed25519; signature over "<prev_hash>:<payload_hash>")
"""


def build_bundle_zip(
    job: JobView,
    settings: Settings,
    *,
    audit_pdf: bytes | None = None,
    card_png: bytes | None = None,
) -> bytes:
    """bundle.zip for download / attaching to reports. `audit_pdf` is added when the caller has it."""
    from .card import render_report_card  # local import: Pillow work only when a zip is requested

    bundle = build_bundle(job, settings)
    files: dict[str, bytes] = {
        "verdict.json": _json_bytes(bundle.verdict),
        "sources.json": _json_bytes(bundle.sources),
        "forensic_report.json": _json_bytes(bundle.forensic_report),
        "media_hashes.json": _json_bytes(bundle.media_hashes),
        "report_card.png": card_png if card_png is not None else render_report_card(job, settings),
    }
    if audit_pdf is not None:
        files["audit_record.pdf"] = audit_pdf
    manifest = {
        "job_id": job.job_id,
        "audit_id": bundle.audit_id,
        "timestamps": bundle.timestamps,
        "pdf_url": bundle.pdf_url,
        "verify_url": bundle.verify_url,
        "audit_page_url": links.audit_page_url(settings, job.job_id),
        "files_sha256": {name: sha256_hex(data) for name, data in files.items()},
    }
    files["bundle.json"] = _json_bytes(manifest)
    files["README.txt"] = README.format(
        verify_url=bundle.verify_url, pubkey_url=links.pubkey_url(settings)
    ).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()
