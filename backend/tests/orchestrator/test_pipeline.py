"""Orchestrator graph + runner: happy path, shortcuts, failure policy (E6), parallelism, concurrency."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.orchestrator.builders import FakeServices
from veriloop.contracts import (
    AbstainReason,
    ForensicStatus,
    JobStatus,
    VerdictLabel,
    VerifyRequest,
)
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.orchestrator import JobRunner, ServiceSet, default_services
from veriloop.orchestrator.nodes import BRANCHES, PIPELINE
from veriloop.orchestrator.services import ServiceUnavailable, resolve_service
from veriloop.services.audit import get_entry, verify_chain

TEXT = VerifyRequest(text="This video shows Cyclone Arnab hitting Visakhapatnam.", source="web")
URL = VerifyRequest(url="https://x.com/someone/status/123", source="web")


async def run_one(deps: Deps, fakes: FakeServices, request: VerifyRequest = URL, **kw: object):
    runner = JobRunner(deps, fakes.service_set(**kw))  # type: ignore[arg-type]
    await runner.start()
    try:
        job_id = await runner.submit(request)
        view = await runner.wait(job_id, timeout=30)
    finally:
        await runner.shutdown()
    assert view is not None
    return view, deps.events.history(job_id)


async def test_happy_path_runs_every_node_and_audits(deps: Deps) -> None:
    fakes = FakeServices(label=VerdictLabel.FALSE)
    view, events = await run_one(deps, fakes)

    assert view.status is JobStatus.COMPLETED
    assert view.verdict is VerdictLabel.FALSE
    assert view.report_eligible is True and view.legal_eligible is False
    assert view.legal_block_reason and "MANIPULATED" in view.legal_block_reason
    ran = {t.node: t for t in view.trace}
    assert set(ran) == set(PIPELINE)
    assert all(t.status == "ok" for t in ran.values())
    assert "run_debate" in fakes.calls
    # cited first, then the rest (incl. provenance items)
    assert [s.id for s in view.sources][:2] == ["E1", "E2"] and "P1" in [s.id for s in view.sources]
    assert view.audit_id is not None
    entry = await get_entry(deps, view.job_id)
    assert entry is not None and entry.id == view.audit_id
    assert (await verify_chain(deps, view.job_id)).ok

    node_events = [e for e in events if e.get("type") == "node"]
    assert {e["node"] for e in node_events} >= set(PIPELINE)
    assert all("duration_ms" in e for e in node_events if e["status"] == "end")
    assert events[-1]["type"] == "done" and events[-1]["verdict"] == "FALSE"
    statuses = [e["status"] for e in events if e.get("type") == "status"]
    assert statuses[0] == "queued" and "processing" in statuses and statuses[-1] == "completed"
    assert deps.events.is_closed(view.job_id)


async def test_not_checkable_shortcut_skips_branches(deps: Deps) -> None:
    fakes = FakeServices(worthiness=0.1)
    view, events = await run_one(deps, fakes, TEXT)
    assert view.verdict is VerdictLabel.NOT_CHECKABLE
    assert not {"gather_evidence", "run_forensics", "run_provenance", "run_debate", "fuse"} & set(fakes.calls)
    skipped = {t.node for t in view.trace if t.status == "skipped"}
    assert skipped == {*BRANCHES, "fuse_evidence", "judge"}
    assert all(t.detail and t.detail.startswith("skipped") for t in view.trace if t.status == "skipped")
    assert any(e.get("detail", "").startswith("skipped") for e in events if e.get("type") == "node")
    assert view.report_eligible is False and view.audit_id is not None


async def test_not_checkable_without_fusion_package_falls_back_locally(deps: Deps) -> None:
    view, _ = await run_one(deps, FakeServices(claims=False), TEXT, missing=("decide", "fuse"))
    assert view.verdict is VerdictLabel.NOT_CHECKABLE
    assert view.abstain_detail and "No factual claim" in view.abstain_detail
    assert not [e for e in view.tool_errors if e.tool.startswith("node:")]


async def test_unverifiable_candidate_skips_debate(deps: Deps) -> None:
    fakes = FakeServices(candidate=VerdictLabel.UNVERIFIABLE)
    view, _ = await run_one(deps, fakes)
    assert view.verdict is VerdictLabel.UNVERIFIABLE
    assert "run_debate" not in fakes.calls
    judge = next(t for t in view.trace if t.node == "judge")
    assert judge.status == "skipped" and "cheap abstain" in (judge.detail or "")


@pytest.mark.parametrize("stage", ["gather_evidence", "resolve_input", "run_debate", "decide"])
async def test_node_exception_ends_tool_outage_and_is_audited(deps: Deps, stage: str) -> None:
    fakes = FakeServices(fail={stage: RuntimeError(f"{stage} exploded")})
    view, events = await run_one(deps, fakes)
    assert view.status is JobStatus.COMPLETED
    assert view.verdict is VerdictLabel.UNVERIFIABLE
    assert view.abstain_reason is AbstainReason.TOOL_OUTAGE
    assert view.report_eligible is False
    assert any(e.tool.startswith("node:") and "exploded" in e.message for e in view.tool_errors)
    assert any("verdict withheld" in label for label in view.labels)
    assert view.audit_id is not None and (await get_entry(deps, view.job_id)) is not None
    if stage != "decide":
        assert any(e.get("status") == "error" for e in events if e.get("type") == "node")


async def test_missing_service_package_is_a_clean_outage(deps: Deps) -> None:
    view, _ = await run_one(deps, FakeServices(), missing=("resolve_input",))
    assert view.verdict is VerdictLabel.UNVERIFIABLE and view.abstain_reason is AbstainReason.TOOL_OUTAGE
    err = next(e for e in view.tool_errors if e.tool == "node:resolve_input")
    assert "ServiceUnavailable" in err.message
    assert view.input is not None and view.input.url == URL.url  # falls back to the request


def test_lazy_services_report_missing_modules() -> None:
    with pytest.raises(ServiceUnavailable):
        resolve_service("veriloop.services.does_not_exist", "resolve_input")
    services = default_services()
    assert callable(services.resolve_input) and callable(services.fuse)
    with pytest.raises(ServiceUnavailable):
        ServiceSet().get("decide")


async def test_parallel_branches_overlap_in_time(deps: Deps) -> None:
    fakes = FakeServices(delay=0.25)
    view, _ = await run_one(deps, fakes)
    windows = [fakes.windows[n] for n in ("gather_evidence", "run_forensics", "run_provenance")]
    latest_start = max(w[0] for w in windows)
    earliest_end = min(w[1] for w in windows)
    assert latest_start < earliest_end, f"branches did not overlap: {windows}"
    timings = {t.node: t for t in view.trace if t.node in BRANCHES}
    assert max(t.started_at for t in timings.values()) < min(t.ended_at for t in timings.values())  # type: ignore[type-var]


async def test_five_concurrent_jobs(deps: Deps) -> None:
    fakes = FakeServices(delay=0.2)
    runner = JobRunner(deps, fakes.service_set(), max_concurrent=5)
    await runner.start()
    try:
        t0 = time.perf_counter()
        ids = [await runner.submit(URL) for _ in range(5)]
        views = await asyncio.gather(*(runner.wait(i, timeout=30) for i in ids))
        elapsed = time.perf_counter() - t0
    finally:
        await runner.shutdown()
    assert all(v is not None and v.status is JobStatus.COMPLETED for v in views)
    # one job = 4 sequential stages x 0.2 s ~= 0.8 s; five sequential runs would take >= 4 s
    assert elapsed < 3.0, elapsed
    assert len({v.audit_id for v in views if v}) == 5
    assert (await verify_chain(deps)).ok


async def test_queue_positions_when_saturated(deps: Deps) -> None:
    fakes = FakeServices(delay=0.1)
    runner = JobRunner(deps, fakes.service_set(), max_concurrent=1)
    await runner.start()
    try:
        ids = [await runner.submit(URL) for _ in range(3)]  # first runs, second is next, third waits
        third = await runner.get_view(ids[2])
        assert third is not None and third.status is JobStatus.QUEUED
        assert third.queue_position == 1 and third.status_detail == "queued (1 job(s) ahead)"
        for job_id in ids:
            view = await runner.wait(job_id, timeout=30)
            assert view is not None and view.status is JobStatus.COMPLETED
    finally:
        await runner.shutdown()
    positions = [e.get("queue_position") for e in deps.events.history(ids[2]) if e.get("status") == "queued"]
    assert positions == [1, 0]


async def test_rate_limited_status_detail(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = JobRunner(deps, FakeServices().service_set())
    monkeypatch.setattr(deps.tools, "rate_limited_tools", lambda within_s=60.0: ["gdelt"])
    assert runner.processing_detail() == "processing (rate-limited: gdelt)"


async def test_citation_gate_downgrades_single_source(deps: Deps) -> None:
    fakes = FakeServices()
    original = fakes.decide

    def one_source(*args, **kwargs):  # type: ignore[no-untyped-def]
        v = original(*args, **kwargs)
        return v.model_copy(update={"cited_evidence_ids": ["E1", "E9"]})

    fakes.decide = one_source  # type: ignore[method-assign]
    view, _ = await run_one(deps, fakes)
    assert view.verdict is VerdictLabel.UNVERIFIABLE
    assert view.abstain_reason is AbstainReason.SINGLE_SOURCE
    assert "Citation gate" in (view.abstain_detail or "")


@pytest.mark.parametrize(
    ("private", "forensic", "groups", "eligible"),
    [
        (False, ForensicStatus.INCONCLUSIVE, 2, True),  # public figure
        (True, ForensicStatus.MANIPULATED, 2, True),
        (True, ForensicStatus.INCONCLUSIVE, 3, False),  # G9: forensics must agree
        (None, ForensicStatus.MANIPULATED, 1, False),  # G9: unknown status -> treat as private
    ],
)
async def test_decide_action_sets_legal_eligibility(
    deps: Deps, private: bool | None, forensic: ForensicStatus, groups: int, eligible: bool
) -> None:
    fakes = FakeServices(
        label=VerdictLabel.MANIPULATED,
        identifiable=True,
        private=private,
        forensic_status=forensic,
        groups_manipulation=groups,
    )
    view, _ = await run_one(deps, fakes)
    assert view.verdict is VerdictLabel.MANIPULATED
    assert view.legal_eligible is eligible
    assert (view.legal_block_reason is None) is eligible


async def test_startup_fails_interrupted_jobs(deps: Deps) -> None:
    from veriloop.core.ids import new_job_id

    stale = new_job_id()
    await repo.create_job(deps.db, stale, request=URL, status=JobStatus.PROCESSING)
    runner = JobRunner(deps, FakeServices().service_set())
    await runner.start()
    await runner.shutdown()
    row = await repo.get_job(deps.db, stale)
    assert row is not None and row.status is JobStatus.FAILED and row.error


async def test_completed_view_is_persisted(deps: Deps) -> None:
    view, _ = await run_one(deps, FakeServices())
    row = await repo.get_job(deps.db, view.job_id)
    assert row is not None and row.status is JobStatus.COMPLETED
    assert (
        row.result
        and row.result["verdict"] == "FALSE"
        and row.state
        and row.state["verdict"]["label"] == "FALSE"
    )
    assert row.audit_id == view.audit_id
