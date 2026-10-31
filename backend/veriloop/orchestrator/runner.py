"""In-process job runner (D-07): Postgres `jobs` rows + asyncio tasks + a concurrency semaphore.

Lifecycle: submit() -> row `queued` (+ SSE status with queue position) -> waits on
Semaphore(MAX_CONCURRENT_JOBS) -> `processing` (status_detail names any rate-limited tool, E10) ->
graph run (live state kept in memory for GET /verify/{id} and SSE) -> `completed` with result_json
(the JobView) and state_json, or `failed` with an error. A final {"type":"done"} event is published
and the job's event stream is closed. At startup, rows left queued/processing by a previous process
are marked failed (they can never finish).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel

from veriloop.contracts import JobStatus, JobView, VerifyRequest, utcnow
from veriloop.core.hashing import sha256_hex
from veriloop.core.ids import new_job_id
from veriloop.db import repo
from veriloop.deps import Deps

from .graph import build_graph
from .jobview import build_job_view, view_json
from .services import ServiceSet

log = structlog.get_logger(__name__)

RATE_POLL_S = 1.0


@dataclass
class JobMeta:
    job_id: str
    created_at: datetime
    source: str
    mode: str


def dump_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-ready copy of a JobState (pydantic models dumped) for jobs.state_json."""

    def dump(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, list | tuple):
            return [dump(v) for v in value]
        if isinstance(value, dict):
            return {k: dump(v) for k, v in value.items()}
        return value

    return {k: dump(v) for k, v in state.items()}


def is_private(request: VerifyRequest | None) -> bool:
    """Telegram jobs: user content may only live in TTL storage (G6/D7/D-14)."""
    return request is not None and request.source == "telegram"


def persisted_request(request: VerifyRequest) -> dict[str, Any]:
    """jobs.request_json. The runner keeps the real request in memory (interrupted jobs are failed, never
    resumed), so Telegram jobs persist only hashes of the message content; the raw text/URL lives in
    ephemeral_inputs until the TTL purge."""
    data = request.model_dump(mode="json")
    if not is_private(request):
        return data
    for key in ("url", "text"):
        value = data.pop(key, None)
        data[f"{key}_sha256"] = sha256_hex(value) if value else None
    return data


class JobRunner:
    def __init__(
        self, deps: Deps, services: ServiceSet | None = None, *, max_concurrent: int | None = None
    ) -> None:
        self.deps = deps
        self.max_concurrent = max_concurrent or deps.settings.max_concurrent_jobs
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._graph = build_graph(deps, services, self._audit_view)
        self._meta: dict[str, JobMeta] = {}
        self._live: dict[str, dict[str, Any]] = {}
        self._queued: list[str] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._rate_task: asyncio.Task[None] | None = None
        self._rate_state: tuple[str, ...] = ()

    # ---- lifecycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        failed = await repo.fail_interrupted_jobs(self.deps.db)
        if failed:
            log.warning("interrupted_jobs_failed", count=failed)
        self._rate_task = asyncio.create_task(self._rate_limit_monitor(), name="rate-limit-monitor")
        log.info("job_runner_started", max_concurrent=self.max_concurrent)

    async def shutdown(self) -> None:
        if self._rate_task is not None:
            self._rate_task.cancel()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if self._rate_task is not None:
            tasks.append(self._rate_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("job_runner_stopped", cancelled=len(tasks))

    @property
    def running(self) -> int:
        return len(self._live)

    # ---- submission --------------------------------------------------------------------------
    async def submit(
        self,
        request: VerifyRequest,
        upload_path: str | None = None,
        *,
        job_id: str | None = None,
        source_ref_hmac: str | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        job_id = job_id or new_job_id()
        settings = self.deps.settings
        row = await repo.create_job(
            self.deps.db,
            job_id,
            request=persisted_request(request),
            source=request.source,
            mode=settings.tool_mode,
            expires_at=expires_at,
            source_ref_hmac=source_ref_hmac,
        )
        self._meta[job_id] = JobMeta(job_id, row.created_at, request.source, settings.tool_mode)
        self._queued.append(job_id)
        position = self._queued.index(job_id)
        self._publish(
            job_id,
            {
                "type": "status",
                "status": JobStatus.QUEUED.value,
                "queue_position": position,
                "status_detail": self._queued_detail(position),
            },
        )
        log.info(
            "job_submitted",
            job_id=job_id,
            source=request.source,
            queue_position=position,
            has_upload=upload_path is not None,
        )
        task = asyncio.create_task(self._run(job_id, request, upload_path), name=f"job-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))
        return job_id

    # `timeout` stays a plain argument (callers such as the Telegram flow pass it through).
    async def wait(self, job_id: str, timeout: float | None = None) -> JobView | None:  # noqa: ASYNC109
        """Wait until the job finishes (if it runs in this process) and return its view."""
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        return await self.get_view(job_id)

    def queue_position(self, job_id: str) -> int | None:
        return self._queued.index(job_id) if job_id in self._queued else None

    # ---- views -------------------------------------------------------------------------------
    def processing_detail(self) -> str:
        limited = self.deps.tools.rate_limited_tools()
        return f"processing (rate-limited: {', '.join(sorted(limited))})" if limited else "processing"

    @staticmethod
    def _queued_detail(position: int) -> str:
        return "queued (next to run)" if position == 0 else f"queued ({position} job(s) ahead)"

    def view_from_state(
        self, job_id: str, state: Mapping[str, Any], status: JobStatus, *, status_detail: str | None = None
    ) -> JobView:
        meta = self._meta.get(job_id)
        request = state.get("request")
        return build_job_view(
            state,
            job_id=job_id,
            status=status,
            created_at=meta.created_at if meta else utcnow(),
            updated_at=utcnow(),
            source=meta.source if meta else (request.source if request else "web"),
            mode=meta.mode if meta else self.deps.settings.tool_mode,
            settings=self.deps.settings,
            resolved_models=self.deps.resolved_models,
            status_detail=status_detail,
        )

    def _audit_view(self, state: Mapping[str, Any]) -> JobView:
        return self.view_from_state(state["job_id"], state, JobStatus.COMPLETED)

    def _minimal_view(self, row: repo.JobRow, **extra: Any) -> JobView:
        return JobView(
            job_id=row.job_id,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
            source=row.source,  # type: ignore[arg-type]
            mode=row.mode,
            labels=self.deps.settings.mode_labels(),
            resolved_models=dict(self.deps.resolved_models),
            redacted=row.redacted,
            error=row.error,
            audit_id=row.audit_id,
            **extra,
        )

    async def get_view(self, job_id: str) -> JobView | None:
        if job_id in self._live:
            return self.view_from_state(
                job_id, self._live[job_id], JobStatus.PROCESSING, status_detail=self.processing_detail()
            )
        row = await repo.get_job(self.deps.db, job_id)
        if row is None:
            return None
        if row.status is JobStatus.QUEUED:
            position = self.queue_position(job_id)
            if position is None:
                position = await repo.queue_position(self.deps.db, job_id)
            return self._minimal_view(
                row, queue_position=position, status_detail=self._queued_detail(position)
            )
        if row.result:
            view = JobView.model_validate(row.result)
            return view.model_copy(
                update={
                    "status": row.status,
                    "updated_at": row.updated_at,
                    "audit_id": row.audit_id if row.audit_id is not None else view.audit_id,
                    "redacted": row.redacted,
                    "error": row.error or view.error,
                    "queue_position": None,
                    "status_detail": None,
                }
            )
        detail = self.processing_detail() if row.status is JobStatus.PROCESSING else None
        return self._minimal_view(row, status_detail=detail)

    # ---- execution ---------------------------------------------------------------------------
    def _publish(self, job_id: str, event: dict[str, Any]) -> None:
        try:
            self.deps.events.publish(job_id, event)
        except Exception as exc:
            log.warning("event_publish_failed", job_id=job_id, error=str(exc))

    def _dequeue(self, job_id: str) -> None:
        if job_id not in self._queued:
            return
        self._queued.remove(job_id)
        for position, other in enumerate(self._queued):
            self._publish(
                other,
                {
                    "type": "status",
                    "status": JobStatus.QUEUED.value,
                    "queue_position": position,
                    "status_detail": self._queued_detail(position),
                },
            )

    async def _run(self, job_id: str, request: VerifyRequest, upload_path: str | None) -> None:
        t0 = time.perf_counter()
        try:
            async with self._sem:
                self._dequeue(job_id)
                await repo.update_job(self.deps.db, job_id, status=JobStatus.PROCESSING, started_at=utcnow())
                self._live[job_id] = {
                    "job_id": job_id,
                    "request": request,
                    "upload_path": upload_path,
                    "trace": [],
                    "tool_errors": [],
                }
                self._publish(
                    job_id,
                    {
                        "type": "status",
                        "status": JobStatus.PROCESSING.value,
                        "status_detail": self.processing_detail(),
                    },
                )
                log.info("job_started", job_id=job_id, running=len(self._live))
                async for values in self._graph.astream(
                    self._live[job_id], stream_mode="values", config={"recursion_limit": 50}
                ):
                    self._live[job_id] = values
                await self._complete(job_id, self._live[job_id], t0)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._fail(job_id, "cancelled: the server shut down while the job was running")
            )
            raise
        except Exception as exc:
            log.exception("job_crashed", job_id=job_id)
            await self._fail(job_id, f"{type(exc).__name__}: {exc}"[:500])
        finally:
            self._dequeue(job_id)
            self._live.pop(job_id, None)
            self._meta.pop(job_id, None)
            self.deps.events.close(job_id)

    async def _complete(self, job_id: str, state: Mapping[str, Any], t0: float) -> None:
        view = self.view_from_state(job_id, state, JobStatus.COMPLETED)
        await repo.update_job(
            self.deps.db,
            job_id,
            status=JobStatus.COMPLETED,
            completed_at=utcnow(),
            # Telegram (D7): the raw graph state (caption, transcript...) is not kept; the view is, until TTL.
            state=None if is_private(state.get("request")) else dump_state(state),
            result=view_json(view),
            verdict=view.verdict.value if view.verdict else None,
            confidence=view.confidence,
            audit_id=view.audit_id,
        )
        self._live.pop(job_id, None)
        self._publish(job_id, {"type": "status", "status": JobStatus.COMPLETED.value})
        self._publish(
            job_id,
            {
                "type": "done",
                "job_id": job_id,
                "status": JobStatus.COMPLETED.value,
                "verdict": view.verdict.value if view.verdict else None,
                "audit_id": view.audit_id,
            },
        )
        log.info(
            "job_completed",
            job_id=job_id,
            verdict=view.verdict.value if view.verdict else None,
            confidence=view.confidence,
            abstain_reason=view.abstain_reason.value if view.abstain_reason else None,
            audit_id=view.audit_id,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            degraded_tools=view.degraded_tools,
        )

    async def _fail(self, job_id: str, error: str) -> None:
        try:
            await repo.update_job(
                self.deps.db, job_id, status=JobStatus.FAILED, error=error, completed_at=utcnow()
            )
        except Exception as exc:
            log.error("job_fail_update_failed", job_id=job_id, error=str(exc))
        self._publish(job_id, {"type": "status", "status": JobStatus.FAILED.value, "status_detail": error})
        self._publish(
            job_id, {"type": "done", "job_id": job_id, "status": JobStatus.FAILED.value, "error": error}
        )
        log.warning("job_failed", job_id=job_id, error=error)

    async def _rate_limit_monitor(self) -> None:
        """Push "processing (rate-limited: <tool>)" to running jobs whenever the set changes (E10)."""
        while True:
            await asyncio.sleep(RATE_POLL_S)
            try:
                limited = tuple(sorted(self.deps.tools.rate_limited_tools()))
                if limited == self._rate_state:
                    continue
                self._rate_state = limited
                detail = self.processing_detail()
                for job_id in list(self._live):
                    self._publish(
                        job_id,
                        {"type": "status", "status": JobStatus.PROCESSING.value, "status_detail": detail},
                    )
                if limited:
                    log.info("rate_limited_status", tools=list(limited), running=len(self._live))
            except Exception as exc:  # never let the monitor die
                log.warning("rate_limit_monitor_error", error=str(exc))
