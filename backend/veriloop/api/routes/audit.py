"""Public audit endpoints (SPEC §9): chain entry, signed PDF, chain verification, public key.

Public on purpose (no API key): anyone holding a report card / QR code must be able to check the
record independently. The browser re-checks sha256(payload_json) == payload_hash and the base64
Ed25519 signature over "prev_hash:payload_hash" with GET /audit/pubkey (PEM SubjectPublicKeyInfo).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from veriloop.contracts import AuditEntryView, AuditEventType, AuditVerifyResult
from veriloop.core.errors import ProblemError
from veriloop.core.executor import run_cpu
from veriloop.core.ids import is_job_id
from veriloop.db import repo
from veriloop.services.audit import get_entry, public_key_pem, render_pdf, verify_chain

from ..runtime import AppRuntime, get_runtime

router = APIRouter(tags=["audit"])


def _no_entry(job_id: str) -> ProblemError:
    return ProblemError(
        404,
        "Audit entry not found",
        f"No audit record for job {job_id!r} yet (it is written when the run finishes).",
        type_="urn:veriloop:problem:audit-not-found",
    )


async def _entry(runtime: AppRuntime, job_id: str, event_type: AuditEventType) -> AuditEntryView:
    if not is_job_id(job_id):
        raise _no_entry(job_id)
    entry = await get_entry(runtime.deps, job_id, event_type)
    if entry is None:
        raise _no_entry(job_id)
    return entry


@router.get("/audit/pubkey", response_class=PlainTextResponse)
async def audit_public_key(request: Request) -> PlainTextResponse:
    """Ed25519 public key, PEM SubjectPublicKeyInfo (text/plain)."""
    pem = public_key_pem(get_runtime(request).deps.settings)
    return PlainTextResponse(pem, headers={"Cache-Control": "public, max-age=300"})


@router.get("/audit/{job_id}", response_model=AuditEntryView)
async def audit_entry(
    job_id: str, request: Request, event_type: AuditEventType = AuditEventType.VERDICT
) -> JSONResponse:
    """Latest chain entry of `event_type` (default verdict) for the job; 404 until it is written."""
    entry = await _entry(get_runtime(request), job_id, event_type)
    return JSONResponse(entry.model_dump(mode="json"))


@router.get(
    "/audit/{job_id}/pdf", response_class=Response, responses={200: {"content": {"application/pdf": {}}}}
)
async def audit_pdf(
    job_id: str, request: Request, event_type: AuditEventType = AuditEventType.VERDICT
) -> Response:
    runtime = get_runtime(request)
    entry = await _entry(runtime, job_id, event_type)
    view = await runtime.runner.get_view(job_id)
    pdf = await run_cpu(render_pdf, entry, view, runtime.deps.settings)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="veriloop-audit-{job_id}-{entry.id}.pdf"',
            "Cache-Control": "no-cache",
        },
    )


@router.get("/audit/{job_id}/verify", response_model=AuditVerifyResult)
async def audit_verify(job_id: str, request: Request) -> JSONResponse:
    """Recompute the whole chain from genesis.

    -> {ok, checked_rows, broken_at, reason, head_hash, job_entry_ids}
    """
    runtime = get_runtime(request)
    db = runtime.deps.db
    known = is_job_id(job_id) and (
        await repo.get_job(db, job_id) is not None
        or await db.fetchval("SELECT 1 FROM audit_chain WHERE job_id = $1 LIMIT 1", job_id) is not None
    )
    if not known:
        raise ProblemError(
            404,
            "Job not found",
            f"No verification job {job_id!r}.",
            type_="urn:veriloop:problem:job-not-found",
        )
    result = await verify_chain(runtime.deps, job_id)
    return JSONResponse(result.model_dump(mode="json"))
