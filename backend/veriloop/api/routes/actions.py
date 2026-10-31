"""Human-gated actions (G5, D4): report package, report confirmation, report card, bundle, legal draft.

Nothing here submits or sends anything: POST /report/{id} only prepares (and audits) the package,
/confirm logs the attempt (no user identity) and returns the presenter-side reporter-agent command,
and /legal/{id}/draft returns a draft after all four acknowledgements.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from veriloop.contracts import (
    AuditEventType,
    JobStatus,
    JobView,
    LegalDraftRequest,
    LegalDraftResponse,
    ReportConfirmRequest,
    ReportConfirmResponse,
    ReportDraftPackage,
)
from veriloop.core.errors import ProblemError
from veriloop.core.executor import run_cpu
from veriloop.services.audit import get_entry, render_pdf
from veriloop.services.legal import generate_legal_draft
from veriloop.services.reporting import (
    attempt_package,
    build_bundle_zip,
    confirm_report,
    prepare_report,
    render_report_card,
)

from ..runtime import get_runtime
from ..security import require_api_key
from .verify import load_view

router = APIRouter(tags=["actions"], dependencies=[Depends(require_api_key)])


def _require_finished(view: JobView) -> None:
    if view.status is not JobStatus.COMPLETED or view.verdict is None:
        raise ProblemError(
            409,
            "Verification not finished",
            f"Job {view.job_id} is {view.status.value}; this needs a completed verdict.",
            type_="urn:veriloop:problem:job-not-finished",
        )


@router.post("/report/{job_id}", response_model=ReportDraftPackage)
async def prepare(job_id: str, request: Request) -> JSONResponse:
    """Evidence bundle + per-platform plans + the exact preview text (audited as report_prepared)."""
    runtime = get_runtime(request)
    view = await load_view(runtime, job_id)
    package = await prepare_report(view, runtime.deps)
    return JSONResponse(package.model_dump(mode="json"))


@router.post("/report/{job_id}/confirm", response_model=ReportConfirmResponse)
async def confirm(job_id: str, body: ReportConfirmRequest, request: Request) -> JSONResponse:
    """The person clicked "I confirm": log the attempt (no identity) and audit it. Never submits (D4)."""
    runtime = get_runtime(request)
    view = await load_view(runtime, job_id)
    response = await confirm_report(view, body, runtime.deps)
    return JSONResponse(response.model_dump(mode="json"))


@router.get("/report/{job_id}/attempts/{attempt_id}")
async def confirmed_attempt(job_id: str, attempt_id: str, request: Request) -> JSONResponse:
    """What reporter-agent fetches: the plan of a CONFIRMED attempt only (the human gate already passed)."""
    runtime = get_runtime(request)
    view = await load_view(runtime, job_id)
    package = await attempt_package(view, attempt_id, runtime.deps)
    if package is None:
        raise ProblemError(
            404,
            "Report attempt not found",
            f"No confirmed report attempt {attempt_id!r} for job {job_id}.",
            type_="urn:veriloop:problem:attempt-not-found",
        )
    return JSONResponse(package)


@router.get(
    "/report/{job_id}/card.png", response_class=Response, responses={200: {"content": {"image/png": {}}}}
)
async def report_card(job_id: str, request: Request) -> Response:
    runtime = get_runtime(request)
    view = await load_view(runtime, job_id)
    _require_finished(view)
    png = await run_cpu(render_report_card, view, runtime.deps.settings)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.get(
    "/report/{job_id}/bundle.zip",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
async def report_bundle(job_id: str, request: Request) -> Response:
    """verdict.json, sources.json, forensic_report.json, media hashes, card, signed PDF, manifest."""
    runtime = get_runtime(request)
    settings = runtime.deps.settings
    view = await load_view(runtime, job_id)
    _require_finished(view)
    entry = await get_entry(runtime.deps, job_id, AuditEventType.VERDICT)
    pdf = await run_cpu(render_pdf, entry, view, settings) if entry is not None else None
    data = await run_cpu(lambda: build_bundle_zip(view, settings, audit_pdf=pdf))
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="veriloop-{job_id}-bundle.zip"',
            "Cache-Control": "no-cache",
        },
    )


@router.post("/legal/{job_id}/draft", response_model=LegalDraftResponse)
async def legal_draft(job_id: str, body: LegalDraftRequest, request: Request) -> JSONResponse:
    """Jurisdiction draft (IN/EU/US) after all four acknowledgements (else 422). Draft only, never sent."""
    runtime = get_runtime(request)
    view = await load_view(runtime, job_id)
    response = await generate_legal_draft(view, body, runtime.deps)
    return JSONResponse(response.model_dump(mode="json"))
