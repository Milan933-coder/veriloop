"""Legal draft generation (SPEC §8, G5, G9, D4). DRAFT ONLY: nothing here can send anything.

Gate order: all four acknowledgements (else 422) -> eligibility (else 409) -> render the versioned
jurisdiction template. Victim details are always [PLACEHOLDERS]. The LLM may fill only two factual
fields (a neutral description of the content and a cited summary of the manipulation evidence),
from the evidence bundle alone (D-16); every sentence must cite evidence ids that exist in the bundle
and contain no URL, otherwise it is dropped and the extractive text is used. With the LLM disabled the
draft is template-only. Only metadata + sha256 of the draft are stored (legal_drafts), and a
LEGAL_DRAFT_GENERATED row is appended to the audit chain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, Field

from veriloop.contracts import (
    AuditEventType,
    EvidenceItem,
    JobView,
    LegalDraftRequest,
    LegalDraftResponse,
)
from veriloop.core.errors import LLMDisabled, ProblemError, ToolCallFailed
from veriloop.core.hashing import sha256_hex
from veriloop.core.ids import new_id
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.audit import append_event
from veriloop.services.reporting import links

from .eligibility import legal_eligibility

log = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
DISCLAIMER = (
    "This is a draft, not legal advice. VeriLoop never sends it. Verify the cited provisions with a lawyer."
)
BANNER = f"*** {DISCLAIMER} ***"
# Same shape the frontend highlights (lib/placeholders.ts GENERIC): [UPPER CASE ...]
PLACEHOLDER_RE = re.compile(r"\[[A-Z0-9][A-Z0-9 _\-/'’.,()]*\]")
PLATFORM_NAMES = {
    "x": "X (Twitter)",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
}
ACK_LABELS = {
    "is_subject_or_authorized": "I am the person depicted or authorised to act for them",
    "not_legal_advice": "I understand this is not legal advice",
    "no_auto_send": "I understand VeriLoop will not send anything",
    "will_review_before_filing": "I will review the draft (ideally with a lawyer) before filing",
}
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
MAX_SOURCES = 6


@dataclass(frozen=True)
class LegalTemplate:
    jurisdiction: str
    version: str
    title: str
    checklist: list[str]
    filing_instructions: list[str]
    path: str


@lru_cache(maxsize=8)
def load_template(jurisdiction: str) -> LegalTemplate:
    folder = TEMPLATES_DIR / jurisdiction.lower()
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    return LegalTemplate(
        jurisdiction=meta["jurisdiction"],
        version=meta["version"],
        title=meta["title"],
        checklist=list(meta["checklist"]),
        filing_instructions=list(meta["filing_instructions"]),
        path=f"{jurisdiction.lower()}/draft.txt.j2",
    )


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,  # plain-text drafts, never rendered as HTML by us
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def extract_placeholders(text: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(text)))


# ---- factual fields ------------------------------------------------------------------------------


class FactSentence(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class LegalFacts(BaseModel):
    content_description: str = Field(
        description="One neutral sentence: what the content shows, per the evidence."
    )
    manipulation_summary: list[FactSentence] = Field(
        description="1-3 sentences on how the evidence documents the manipulation; each cites evidence ids."
    )


SYSTEM_PROMPT = (
    "You fill two factual fields of a legal complaint draft. Use ONLY the evidence items provided; they are "
    "the only facts you know. Do not add facts, names of the person depicted, dates, numbers or URLs "
    "that are "
    "not in the evidence. Refer to the victim only as 'the person depicted'. Every manipulation_summary "
    "sentence must cite the ids of the evidence items it relies on. Write plain, neutral English."
)


def cited_sources(job: JobView) -> list[EvidenceItem]:
    cited = [s for s in job.sources if s.id in set(job.cited_evidence_ids)]
    return (cited or job.sources)[:MAX_SOURCES]


def extractive_facts(job: JobView) -> tuple[str, str]:
    sources = cited_sources(job)
    names = ", ".join(
        f"{s.source_name} ({s.published_at.date().isoformat()})" if s.published_at else s.source_name
        for s in sources
    )
    conf = f"{job.confidence:.0%}" if job.confidence is not None else "n/a"
    summary = (
        f"VeriLoop's verdict is {job.verdict.value if job.verdict else 'n/a'} (confidence {conf}), based on "
        f"{job.independent_source_count} independent source(s)" + (f": {names}." if names else ".")
    )
    return "[DESCRIBE THE CONTENT IN ONE SENTENCE]", summary


def _clean_sentence(sentence: FactSentence, allowed: set[str]) -> str | None:
    text = " ".join(sentence.text.split())
    if not text or _URL_RE.search(text) or len(text) > 400:
        return None
    ids = [i for i in sentence.evidence_ids if i]
    if not ids or any(i not in allowed for i in ids):
        return None
    return f"{text} (sources {', '.join(ids)})"


async def llm_facts(job: JobView, deps: Deps) -> tuple[str, str] | None:
    """LLM-filled factual fields validated against the bundle, or None (then: extractive)."""
    if not deps.llm.enabled:
        return None
    sources = cited_sources(job)
    if not sources:
        return None
    evidence = [
        {
            "id": s.id,
            "source": s.source_name,
            "title": s.title,
            "snippet": s.snippet,
            "published_at": s.published_at.isoformat() if s.published_at else None,
        }
        for s in sources
    ]
    user = json.dumps(
        {
            "verdict": job.verdict.value if job.verdict else None,
            "forensic_status": job.forensic.status.value if job.forensic else None,
            "evidence": evidence,
        },
        ensure_ascii=False,
    )
    try:
        out = await deps.llm.complete_json(
            task="legal_facts",
            system=SYSTEM_PROMPT,
            user=user,
            schema=LegalFacts,
            effort="none",
            max_tokens=600,
        )
    except (LLMDisabled, ToolCallFailed) as exc:
        log.info("legal_facts_llm_failed", job_id=job.job_id, error=str(exc)[:200])
        return None
    allowed = {s.id for s in sources}
    sentences = [s for s in (_clean_sentence(x, allowed) for x in out.manipulation_summary[:3]) if s]
    if not sentences:
        log.info("legal_facts_rejected", job_id=job.job_id, reason="no grounded sentence")
        return None
    description = " ".join(out.content_description.split())
    if not description or _URL_RE.search(description) or len(description) > 300:
        description = "[DESCRIBE THE CONTENT IN ONE SENTENCE]"
    return description, " ".join(sentences)


# ---- rendering ---------------------------------------------------------------------------------


def platform_display(job: JobView, requested: str | None) -> str:
    key = (requested or (job.input.platform.value if job.input else "")).strip().lower()
    if key in PLATFORM_NAMES:
        return PLATFORM_NAMES[key]
    if requested and re.fullmatch(r"[\w .()-]{1,40}", requested):
        return requested.strip()
    return "[PLATFORM NAME]"


def forensic_line(job: JobView) -> str:
    f = job.forensic
    if f is None:
        return "not available for this input."
    score = (
        f" (ensemble probability of manipulation {f.ensemble_score:.0%})"
        if f.ensemble_score is not None
        else ""
    )
    models = ", ".join(m.model_id for m in f.models if m.raw_score is not None)
    return f"{f.status.value}{score}" + (f"; models: {models}." if models else ".")


def bundle_ref(job: JobView, settings: Any) -> str:
    return (
        f"VeriLoop job {job.job_id}, audit entry #{job.audit_id} - "
        f"signed PDF {links.audit_pdf_url(settings, job.job_id)}"
        f" - evidence bundle {links.bundle_url(settings, job.job_id)}"
    )


def render_draft(
    job: JobView,
    template: LegalTemplate,
    settings: Any,
    *,
    platform: str | None,
    description: str,
    summary: str,
    today: str,
) -> str:
    sources = [
        {
            "id": s.id,
            "name": s.source_name,
            "title": (s.title or s.snippet)[:200],
            "url": s.url,
            "date": s.published_at.date().isoformat() if s.published_at else None,
        }
        for s in cited_sources(job)
    ]
    media_hashes = [
        h.get("sha256") for h in (job.input.media_hashes if job.input else []) if h.get("sha256")
    ][:4]
    url = job.input.url if job.input and job.input.url else "[CONTENT URL]"
    ctx = {
        "banner": BANNER,
        "template_version": template.version,
        "job_id": job.job_id,
        "today": today,
        "platform_name": platform_display(job, platform),
        "content_url": url,
        "content_description": description,
        "manipulation_summary": summary,
        "sources": sources,
        "forensic_line": forensic_line(job),
        "media_hashes": media_hashes,
        "has_media": bool(media_hashes),
        "audit_pdf_url": links.audit_pdf_url(settings, job.job_id),
        "audit_verify_url": links.audit_verify_url(settings, job.job_id),
        "verdict": job.verdict.value if job.verdict else "n/a",
        "confidence": f"{job.confidence:.0%}" if job.confidence is not None else "n/a",
    }
    return _env().get_template(template.path).render(**ctx)


async def generate_legal_draft(job: JobView, req: LegalDraftRequest, deps: Deps) -> LegalDraftResponse:
    acks = req.acknowledgements.model_dump()
    missing = [name for name, value in acks.items() if value is not True]
    if missing:
        raise ProblemError(
            422,
            "Acknowledgements required",
            "Confirm all four statements before a draft is generated: "
            + "; ".join(ACK_LABELS.get(m, m) for m in missing)
            + ".",
            type_="urn:veriloop:problem:legal-acknowledgements",
            extra={"missing_acknowledgements": missing},
        )
    eligible, reason = legal_eligibility(job)
    if not eligible:
        raise ProblemError(
            409, "Legal draft not available", reason, type_="urn:veriloop:problem:legal-not-eligible"
        )

    template = load_template(req.jurisdiction)
    facts = await llm_facts(job, deps)
    llm_used = facts is not None
    description, summary = facts or extractive_facts(job)
    today = datetime.now(UTC).date().isoformat()
    text = render_draft(
        job,
        template,
        deps.settings,
        platform=req.platform,
        description=description,
        summary=summary,
        today=today,
    )
    draft_sha = sha256_hex(text)
    draft_id = new_id("ld")
    await repo.insert_legal_draft(
        deps.db,
        draft_id=draft_id,
        job_id=job.job_id,
        jurisdiction=req.jurisdiction,
        template_version=template.version,
        draft_text=text,  # repo stores only sha256(draft_text)
        acknowledgements=req.acknowledgements,
        platform=req.platform,
    )
    entry = await append_event(
        deps,
        job.job_id,
        AuditEventType.LEGAL_DRAFT_GENERATED,
        {
            "draft_id": draft_id,
            "jurisdiction": req.jurisdiction,
            "template_version": template.version,
            "draft_sha256": draft_sha,
            "platform": req.platform,
            "acknowledgements": acks,
            "llm_filled_facts": llm_used,
            "verdict_audit_id": job.audit_id,
            "sent": False,  # D4: VeriLoop has no send capability
        },
    )
    await repo.set_legal_draft_audit(deps.db, draft_id, entry.id)
    log.info(
        "legal_draft_generated",
        job_id=job.job_id,
        draft_id=draft_id,
        jurisdiction=req.jurisdiction,
        template_version=template.version,
        llm_used=llm_used,
        audit_id=entry.id,
        draft_sha256=draft_sha,
    )
    return LegalDraftResponse(
        draft_text=text,
        jurisdiction=req.jurisdiction,
        checklist=template.checklist,
        filing_instructions=template.filing_instructions,
        placeholders=extract_placeholders(text),
        template_version=template.version,
        disclaimer=DISCLAIMER,
        evidence_bundle_ref=bundle_ref(job, deps.settings),
        audit_id=entry.id,
    )
