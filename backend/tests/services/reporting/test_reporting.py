"""Reporting: eligibility, bundle, plans per platform, human-gated confirm, card PNG, bundle.zip."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from PIL import Image

from tests.orchestrator.builders import make_view
from veriloop.contracts import (
    AuditEventType,
    JobStatus,
    JobView,
    Platform,
    ReportConfirmRequest,
    VerdictLabel,
    VerifyRequest,
)
from veriloop.core.errors import ProblemError
from veriloop.core.hashing import sha256_hex
from veriloop.core.ids import new_job_id
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.audit import get_entry
from veriloop.services.reporting import (
    attempt_package,
    build_bundle,
    build_bundle_zip,
    build_plans,
    confirm_report,
    prepare_report,
    preview_text,
    render_report_card,
)


async def _job(deps: Deps, label: VerdictLabel = VerdictLabel.FALSE, **kw: object) -> JobView:
    job_id = new_job_id()
    view = make_view(label, job_id=job_id, **kw)  # type: ignore[arg-type]
    await repo.create_job(
        deps.db,
        job_id,
        request=VerifyRequest(url="https://x.com/a/status/1"),
        source=view.source,
        status=JobStatus.COMPLETED,
    )
    return view


@pytest.mark.parametrize(
    ("label", "eligible"),
    [
        (VerdictLabel.FALSE, True),
        (VerdictLabel.MANIPULATED, True),
        (VerdictLabel.TRUE, False),
        (VerdictLabel.UNVERIFIABLE, False),
        (VerdictLabel.NOT_CHECKABLE, False),
    ],
)
async def test_prepare_eligibility(deps: Deps, label: VerdictLabel, eligible: bool) -> None:
    job = await _job(deps, label)
    pkg = await prepare_report(job, deps)
    assert pkg.eligible is eligible
    if eligible:
        assert pkg.preview_text and pkg.bundle and pkg.platform_plans and pkg.audit_id
        assert pkg.bundle_url == f"http://testserver/report/{job.job_id}/bundle.zip"
    else:
        assert pkg.ineligible_reason and not pkg.platform_plans and pkg.audit_id is None
        assert await get_entry(deps, job.job_id, AuditEventType.REPORT_PREPARED) is None


async def test_unfinished_job_is_not_reportable(deps: Deps) -> None:
    job = make_view(VerdictLabel.FALSE, status=JobStatus.PROCESSING)
    pkg = await prepare_report(job, deps)
    assert not pkg.eligible and "not finished" in (pkg.ineligible_reason or "")


async def test_prepare_audits_preview_and_bundle(deps: Deps) -> None:
    job = await _job(deps, VerdictLabel.FALSE, audit_id=7)
    pkg = await prepare_report(job, deps)
    entry = await get_entry(deps, job.job_id, AuditEventType.REPORT_PREPARED)
    assert entry is not None and entry.id == pkg.audit_id
    payload = json.loads(entry.payload_json)
    assert payload["preview_text_sha256"] == sha256_hex(pkg.preview_text)
    assert payload["submitted"] is False and payload["verdict_audit_id"] == 7
    assert set(payload["bundle_files_sha256"]) == {
        "verdict.json",
        "sources.json",
        "forensic_report.json",
        "media_hashes.json",
    }
    bundle = pkg.bundle
    assert bundle is not None
    assert bundle.verdict["verdict"] == "FALSE" and bundle.audit_id == 7
    assert [s["id"] for s in bundle.sources][:2] == ["E1", "E2"] and bundle.sources[0]["cited"]
    assert bundle.media_hashes[0]["sha256"] == "ab" * 32 and bundle.media_hashes[0]["phash"]
    assert bundle.pdf_url.endswith(f"/audit/{job.job_id}/pdf") and bundle.verify_url.endswith("/verify")
    assert {"job_created_at", "bundle_generated_at"} <= set(bundle.timestamps)
    for item in job.sources[:2]:
        assert item.url in pkg.preview_text
    assert "never submits" in " ".join(pkg.platform_plans[0].steps).lower()


@pytest.mark.parametrize(
    ("platform", "url", "expected"),
    [
        (
            Platform.X,
            "https://x.com/a/status/1",
            [("x", "playwright_guided"), ("telegram", "report_card"), ("whatsapp", "report_card")],
        ),
        (Platform.INSTAGRAM, "https://www.instagram.com/reel/abc/", [("instagram", "playwright_guided")]),
        (Platform.FACEBOOK, "https://www.facebook.com/reel/1", [("facebook", "playwright_guided")]),
        (Platform.YOUTUBE, "https://www.youtube.com/shorts/x", [("youtube", "playwright_guided")]),
        (Platform.YOUTUBE, None, [("youtube", "deeplink_guided")]),
        (Platform.WHATSAPP, None, [("whatsapp", "report_card"), ("telegram", "report_card")]),
        (Platform.TELEGRAM, "https://t.me/c/1/2", [("telegram", "report_card"), ("whatsapp", "report_card")]),
        (Platform.WEB, "https://example.org/story", [("web", "deeplink_guided")]),
        (Platform.TEXT, None, [("telegram", "report_card"), ("whatsapp", "report_card")]),
    ],
)
def test_plans_per_platform(settings, platform: Platform, url: str | None, expected: list) -> None:  # type: ignore[no-untyped-def]
    job = make_view(VerdictLabel.FALSE, platform=platform, url=url)
    plans = build_plans(job, settings)
    got = [(p.platform, p.method) for p in plans]
    assert got[: len(expected)] == expected
    for p in plans:
        assert p.card_url and p.card_url.endswith("/card.png") and p.evidence_attached
        assert p.prefilled_text and p.steps
        if p.method == "report_card":
            assert any("card" in s.lower() for s in p.steps)
    guided = [p for p in plans if p.method == "playwright_guided"]
    for p in guided:
        assert p.target_url == url and p.report_category


async def test_confirm_requires_human_confirmation(deps: Deps) -> None:
    job = await _job(deps)
    with pytest.raises(ProblemError) as exc:
        await confirm_report(job, ReportConfirmRequest(platform="x", confirmed=False), deps)
    assert exc.value.status == 422
    with pytest.raises(ProblemError) as exc:
        await confirm_report(job, ReportConfirmRequest(platform="myspace", confirmed=True), deps)
    assert exc.value.status == 422 and "available" in (exc.value.detail or "")
    true_job = await _job(deps, VerdictLabel.TRUE)
    with pytest.raises(ProblemError) as exc:
        await confirm_report(true_job, ReportConfirmRequest(platform="x", confirmed=True), deps)
    assert exc.value.status == 409
    assert await repo.list_report_attempts(deps.db, job.job_id) == []


async def test_confirm_logs_attempt_without_identity(deps: Deps) -> None:
    job = await _job(deps, VerdictLabel.MANIPULATED, audit_id=3)
    resp = await confirm_report(job, ReportConfirmRequest(platform="X", confirmed=True), deps)
    assert resp.attempt_logged and resp.platform == "x" and resp.method == "playwright_guided"
    assert resp.reporter_agent_command and resp.attempt_id in resp.reporter_agent_command
    assert "--job " + job.job_id in resp.reporter_agent_command
    rows = await repo.list_report_attempts(deps.db, job.job_id)
    assert len(rows) == 1 and rows[0].audit_id == resp.audit_id and rows[0].target_url == job.input.url
    columns = {
        r["column_name"]
        for r in await deps.db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'report_attempts'"
        )
    }
    assert columns == {
        "attempt_id",
        "job_id",
        "platform",
        "method",
        "status",
        "target_url",
        "audit_id",
        "created_at",
    }  # no user identity anywhere
    entry = await get_entry(deps, job.job_id, AuditEventType.REPORT_CONFIRMED)
    assert entry is not None and entry.id == resp.audit_id
    payload = json.loads(entry.payload_json)
    assert payload["submitted_by_veriloop"] is False and payload["attempt_id"] == resp.attempt_id

    card = await confirm_report(job, ReportConfirmRequest(platform="whatsapp", confirmed=True), deps)
    assert card.method == "report_card" and card.reporter_agent_command is None

    pkg = await attempt_package(job, resp.attempt_id, deps)
    assert pkg is not None and pkg["plan"]["method"] == "playwright_guided" and pkg["status"] == "confirmed"
    assert pkg["preview_text"] == preview_text(job, deps.settings)
    assert await attempt_package(job, "ra_unknown", deps) is None


async def test_telegram_job_report_attempt_keeps_only_hash(deps: Deps) -> None:
    job = await _job(
        deps, VerdictLabel.FALSE, source="telegram", platform=Platform.X, url="https://x.com/p/status/9"
    )
    resp = await confirm_report(job, ReportConfirmRequest(platform="x", confirmed=True), deps)
    rows = await repo.list_report_attempts(deps.db, job.job_id)
    assert rows[0].target_url is None
    entry = await get_entry(deps, job.job_id, AuditEventType.REPORT_CONFIRMED)
    assert entry is not None and "https://x.com/p/status/9" not in entry.payload_json
    assert json.loads(entry.payload_json)["target_url_sha256"] == sha256_hex("https://x.com/p/status/9")
    assert resp.attempt_logged


def test_report_card_png(settings) -> None:  # type: ignore[no-untyped-def]
    job = make_view(VerdictLabel.MANIPULATED, audit_id=12)
    png = render_report_card(job, settings)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size == (1080, 1350)
    hindi = job.model_copy(
        update={
            "claim": job.claim.model_copy(
                update={
                    "claims": [
                        job.claim.claims[0].model_copy(
                            update={"text": "केंद्रीय मंत्रिमंडल ने महंगाई भत्ते में 3 प्रतिशत की बढ़ोतरी"}
                        )
                    ]
                }
            )
        }
    )
    assert render_report_card(hindi, settings).startswith(b"\x89PNG")
    redacted = job.model_copy(update={"claim": None, "redacted": True, "sources": []})
    assert render_report_card(redacted, settings).startswith(b"\x89PNG")


def test_bundle_zip_contents(settings) -> None:  # type: ignore[no-untyped-def]
    job = make_view(VerdictLabel.FALSE, audit_id=5)
    data = build_bundle_zip(job, settings, audit_pdf=b"%PDF-1.4 fake")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert names == {
            "verdict.json",
            "sources.json",
            "forensic_report.json",
            "media_hashes.json",
            "report_card.png",
            "audit_record.pdf",
            "bundle.json",
            "README.txt",
        }
        manifest = json.loads(zf.read("bundle.json"))
        for name, digest in manifest["files_sha256"].items():
            assert sha256_hex(zf.read(name)) == digest
        assert json.loads(zf.read("verdict.json"))["verdict"] == "FALSE"
        assert manifest["audit_id"] == 5 and manifest["pdf_url"].endswith("/pdf")
    bundle = build_bundle(job.model_copy(update={"redacted": True}), settings)
    assert bundle.verdict["claim"] is None and bundle.verdict["claim_redacted"] is True
