"""Prepare / confirm a platform report (SPEC §7, G5 human gate, D4 never submit).

prepare_report: eligibility (FALSE or MANIPULATED only) -> evidence bundle + preview text + plans,
and a REPORT_PREPARED audit row that pins the preview text and bundle file hashes.
confirm_report: requires `confirmed: true` for a platform from the plans, logs a report_attempts row
(no user identity: the table has no such columns and we pass none), appends REPORT_CONFIRMED and
returns the command for the presenter-side reporter-agent, which pre-fills the platform's report
dialog and stops before submit.
"""

from __future__ import annotations

from typing import Any

import structlog

from veriloop.contracts import (
    AuditEventType,
    JobView,
    PlatformPlan,
    ReportConfirmRequest,
    ReportConfirmResponse,
    ReportDraftPackage,
)
from veriloop.core.config import Settings
from veriloop.core.errors import ProblemError
from veriloop.core.hashing import sha256_hex
from veriloop.core.ids import new_id
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.audit import append_event
from veriloop.services.legal.eligibility import report_eligibility

from . import links
from .bundle import build_bundle, bundle_file_hashes
from .plans import build_plans, preview_text

log = structlog.get_logger(__name__)

AGENT_SCRIPT = "reporter-agent/reporter_agent.py"


def agent_command(settings: Settings, job_id: str, attempt_id: str) -> str:
    return f"python {AGENT_SCRIPT} --api {links.base(settings)} --job {job_id} --attempt {attempt_id}"


def _private(job: JobView) -> bool:
    return job.source == "telegram"


def _require_eligible(job: JobView) -> None:
    eligible, reason = report_eligibility(job)
    if not eligible:
        raise ProblemError(
            409, "Report not available", reason, type_="urn:veriloop:problem:report-not-eligible"
        )


async def prepare_report(job: JobView, deps: Deps) -> ReportDraftPackage:
    eligible, reason = report_eligibility(job)
    if not eligible:
        return ReportDraftPackage(job_id=job.job_id, eligible=False, ineligible_reason=reason)
    settings = deps.settings
    bundle = build_bundle(job, settings)
    preview = preview_text(job, settings)
    plans = build_plans(job, settings)
    entry = await append_event(
        deps,
        job.job_id,
        AuditEventType.REPORT_PREPARED,
        {
            "verdict_audit_id": job.audit_id,
            "verdict": job.verdict.value if job.verdict else None,
            "preview_text_sha256": sha256_hex(preview),
            "platforms": [{"platform": p.platform, "method": p.method} for p in plans],
            "bundle_files_sha256": bundle_file_hashes(bundle),
            "submitted": False,  # G5: preparing never submits anything
        },
    )
    log.info("report_prepared", job_id=job.job_id, platforms=[p.platform for p in plans], audit_id=entry.id)
    return ReportDraftPackage(
        job_id=job.job_id,
        eligible=True,
        preview_text=preview,
        bundle=bundle,
        bundle_url=links.bundle_url(settings, job.job_id),
        platform_plans=plans,
        audit_id=entry.id,
    )


def _find_plan(plans: list[PlatformPlan], platform: str) -> PlatformPlan:
    wanted = platform.strip().lower()
    for plan in plans:
        if plan.platform == wanted:
            return plan
    raise ProblemError(
        422,
        "Unknown report platform",
        f"No report plan for {platform!r}; available: {', '.join(p.platform for p in plans)}.",
        type_="urn:veriloop:problem:report-platform",
    )


async def confirm_report(job: JobView, req: ReportConfirmRequest, deps: Deps) -> ReportConfirmResponse:
    if req.confirmed is not True:
        raise ProblemError(
            422,
            "Confirmation required",
            "Review the report text and tick 'I confirm' before a report attempt is logged.",
            type_="urn:veriloop:problem:report-confirmation",
        )
    _require_eligible(job)
    settings = deps.settings
    plan = _find_plan(build_plans(job, settings), req.platform)
    preview = preview_text(job, settings)
    attempt_id = new_id("ra")
    private = _private(job)
    await repo.insert_report_attempt(
        deps.db,
        attempt_id=attempt_id,
        job_id=job.job_id,
        platform=plan.platform,
        method=plan.method,
        status="confirmed",
        # D-14: a Telegram user's link is user content; keep only its hash (in the audit row).
        target_url=None if private else plan.target_url,
    )
    entry = await append_event(
        deps,
        job.job_id,
        AuditEventType.REPORT_CONFIRMED,
        {
            "attempt_id": attempt_id,
            "platform": plan.platform,
            "method": plan.method,
            "target_url": None if private else plan.target_url,
            "target_url_sha256": sha256_hex(plan.target_url) if plan.target_url else None,
            "preview_text_sha256": sha256_hex(preview),
            "verdict_audit_id": job.audit_id,
            "submitted_by_veriloop": False,  # D4: the person submits on the platform
        },
    )
    await repo.set_report_attempt_audit(deps.db, attempt_id, entry.id)
    command = agent_command(settings, job.job_id, attempt_id) if plan.method != "report_card" else None
    log.info(
        "report_confirmed",
        job_id=job.job_id,
        attempt_id=attempt_id,
        platform=plan.platform,
        method=plan.method,
        audit_id=entry.id,
    )
    return ReportConfirmResponse(
        attempt_logged=True,
        attempt_id=attempt_id,
        platform=plan.platform,
        method=plan.method,
        reporter_agent_command=command,
        audit_id=entry.id,
    )


async def attempt_package(job: JobView, attempt_id: str, deps: Deps) -> dict[str, Any] | None:
    """What the reporter-agent fetches: the plan for a CONFIRMED attempt only (the human gate)."""
    attempts = await repo.list_report_attempts(deps.db, job.job_id)
    attempt = next((a for a in attempts if a.attempt_id == attempt_id), None)
    if attempt is None:
        return None
    settings = deps.settings
    plans = build_plans(job, settings)
    plan = next((p for p in plans if p.platform == attempt.platform), None)
    if plan is None:
        return None
    return {
        "attempt_id": attempt.attempt_id,
        "job_id": job.job_id,
        "status": attempt.status,
        "confirmed_at": attempt.created_at.isoformat(),
        "platform": plan.platform,
        "method": plan.method,
        "plan": plan.model_dump(mode="json"),
        "preview_text": preview_text(job, settings),
        "verdict": job.verdict.value if job.verdict else None,
        "bundle_url": links.bundle_url(settings, job.job_id),
        "card_url": links.card_url(settings, job.job_id),
        "audit_page_url": links.audit_page_url(settings, job.job_id),
        "audit_pdf_url": links.audit_pdf_url(settings, job.job_id),
        "audit_id": attempt.audit_id,
    }
