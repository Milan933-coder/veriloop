"""POST /verify (JSON or multipart) -> 202, GET /verify/{id} -> JobView, GET /verify/{id}/events -> SSE."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from sse_starlette.sse import EventSourceResponse

from veriloop.contracts import JobStatus, JobView, VerifyAccepted
from veriloop.core.errors import ProblemError
from veriloop.core.ids import is_job_id, new_job_id
from veriloop.db import repo
from veriloop.orchestrator import view_json

from ..intake import read_json_request, read_multipart
from ..runtime import AppRuntime, get_runtime
from ..security import require_api_key

log = structlog.get_logger(__name__)

SSE_PING_S = 15
TERMINAL = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})


def not_found(job_id: str) -> ProblemError:
    return ProblemError(
        404, "Job not found", f"No verification job {job_id!r}.", type_="urn:veriloop:problem:job-not-found"
    )


async def load_view(runtime: AppRuntime, job_id: str) -> JobView:
    if not is_job_id(job_id):
        raise not_found(job_id)
    view = await runtime.runner.get_view(job_id)
    if view is None:
        raise not_found(job_id)
    return view


def _after_seq(request: Request) -> int:
    raw = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _sse(event: dict[str, Any], *, with_id: bool = True) -> dict[str, str]:
    message = {"data": json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))}
    if with_id and "seq" in event:
        message["id"] = str(event["seq"])
    return message


async def event_stream(runtime: AppRuntime, job_id: str, row: repo.JobRow, after: int) -> AsyncIterator[dict]:
    """Replay + live events (unnamed `data:` JSON messages), always ending with a {"type":"done"} event."""
    bus = runtime.deps.events
    done = False
    finished_elsewhere = row.status in TERMINAL and not bus.is_closed(job_id) and not bus.history(job_id)
    if not finished_elsewhere:
        # Finished jobs whose channel is still known replay their history and end; running ones go live.
        async for event in bus.subscribe(job_id, after_seq=after):
            done = done or event.get("type") == "done"
            yield _sse(event)
    if not done:
        # The job finished in an earlier process (or its history was evicted): summarise from the DB.
        final = await repo.get_job(runtime.deps.db, job_id) or row
        yield _sse({"type": "status", "status": final.status.value}, with_id=False)
        yield _sse(
            {
                "type": "done",
                "job_id": job_id,
                "status": final.status.value,
                "verdict": final.verdict,
                "audit_id": final.audit_id,
                "error": final.error,
            },
            with_id=False,
        )


def build_router(limiter: Limiter, rate_per_min: int) -> APIRouter:
    router = APIRouter(tags=["verify"], dependencies=[Depends(require_api_key)])
    limit = limiter.limit(f"{rate_per_min}/minute") if rate_per_min > 0 else (lambda fn: fn)

    @router.post("/verify", status_code=202, response_model=VerifyAccepted)
    @limit
    async def submit_verification(request: Request) -> JSONResponse:
        """JSON `VerifyRequest`, or multipart with `file` + url/text/platform/locale/source form fields."""
        runtime = get_runtime(request)
        content_type = request.headers.get("content-type", "").lower()
        upload_path: str | None = None
        job_id: str | None = None
        if content_type.startswith("multipart/form-data"):
            job_id = new_job_id()
            verify, upload_path = await read_multipart(request, runtime.deps.settings, job_id)
        elif not content_type or "json" in content_type:
            verify = await read_json_request(request)
        else:
            raise ProblemError(
                415,
                "Unsupported media type",
                "Send application/json or multipart/form-data.",
                type_="urn:veriloop:problem:unsupported-media",
            )
        job_id = await runtime.runner.submit(verify, upload_path, job_id=job_id)
        status_url = f"/verify/{job_id}"
        accepted = VerifyAccepted(
            job_id=job_id,
            status=JobStatus.QUEUED,
            status_url=status_url,
            events_url=f"{status_url}/events",
            queue_position=runtime.runner.queue_position(job_id) or 0,
        )
        return JSONResponse(
            accepted.model_dump(mode="json"), status_code=202, headers={"Location": status_url}
        )

    @router.get("/verify/{job_id}", response_model=JobView)
    async def get_verification(job_id: str, request: Request) -> JSONResponse:
        view = await load_view(get_runtime(request), job_id)
        return JSONResponse(view_json(view))

    @router.get("/verify/{job_id}/events")
    async def verification_events(job_id: str, request: Request) -> EventSourceResponse:
        """Server-sent events: history replay (Last-Event-ID / ?after=) then live, ending with type=done."""
        runtime = get_runtime(request)
        if not is_job_id(job_id):
            raise not_found(job_id)
        row = await repo.get_job(runtime.deps.db, job_id)
        if row is None:
            raise not_found(job_id)
        return EventSourceResponse(
            event_stream(runtime, job_id, row, _after_seq(request)),
            ping=SSE_PING_S,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
