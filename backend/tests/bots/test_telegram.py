"""Telegram: HMAC chat refs, TTL purge/redaction, message flow with fakes (no network)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from tests.orchestrator.builders import FakeServices, make_view
from veriloop.bots.telegram import (
    IncomingMessage,
    TelegramFlow,
    chat_hmac,
    format_result,
    parse_message,
    purge_expired,
    start_telegram,
)
from veriloop.contracts import AbstainReason, JobStatus, VerdictLabel, VerifyRequest, utcnow
from veriloop.core.ids import new_job_id
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.orchestrator import JobRunner, view_json
from veriloop.services.audit import get_entry
from veriloop.services.audit.keys import generate_private_pem

CHAT_ID = 987654321


class FakeReplier:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.edits: list[str] = []
        self.photos: list[tuple[bytes, str]] = []

    async def reply(self, text: str) -> Any:
        self.replies.append(text)
        return len(self.replies)

    async def edit(self, handle: Any, text: str) -> None:
        self.edits.append(text)

    async def send_photo(self, png: bytes, caption: str) -> None:
        self.photos.append((png, caption))


def test_chat_hmac_is_stable_opaque_and_keyed(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings(audit_ed25519_key=generate_private_pem())
    ref = chat_hmac(settings, CHAT_ID)
    assert ref == chat_hmac(settings, str(CHAT_ID)) and len(ref) == 64
    assert str(CHAT_ID) not in ref and ref != chat_hmac(settings, CHAT_ID + 1)
    other = make_settings(audit_ed25519_key=generate_private_pem())
    assert chat_hmac(other, CHAT_ID) != ref  # keyed: unguessable without the server key


def test_parse_message() -> None:
    assert parse_message("Is this real? https://x.com/a/status/1, wow") == (
        "https://x.com/a/status/1",
        "Is this real? , wow",
    )
    assert parse_message("https://youtu.be/abc") == ("https://youtu.be/abc", None)
    assert parse_message("  ") == (None, None) and parse_message(None) == (None, None)


def test_format_result_mentions_verdict_sources_and_audit(settings) -> None:  # type: ignore[no-untyped-def]
    view = make_view(VerdictLabel.FALSE, job_id="1234567890abcdef")
    text = format_result(view, settings)
    assert (
        "VERDICT: FALSE (86% confidence)" in text
        and view.sources[0].url in text
        and view.sources[1].url in text
    )
    assert view.sources[2].url not in text  # top 2 only
    assert "http://testserver/a/1234567890abcdef" in text
    abstain = view.model_copy(
        update={"verdict": VerdictLabel.UNVERIFIABLE, "abstain_reason": AbstainReason.NO_EVIDENCE}
    )
    assert "NOT the same as false" in format_result(abstain, settings)


async def test_flow_submits_private_job_and_replies(deps: Deps) -> None:
    runner = JobRunner(deps, FakeServices(label=VerdictLabel.FALSE).service_set())
    await runner.start()
    try:
        flow = TelegramFlow(deps, runner.submit, runner.wait)
        out = FakeReplier()
        text = "Forwarded: Cyclone Arnab video https://x.com/someone/status/42"
        job_id = await flow.handle(IncomingMessage(chat_id=CHAT_ID, text=text), out)
    finally:
        await runner.shutdown()
    assert job_id is not None
    assert out.replies[0].startswith("Checking")
    assert "VERDICT: FALSE" in out.edits[-1] and "Signed audit record:" in out.edits[-1]
    assert out.photos and out.photos[0][0].startswith(b"\x89PNG")

    row = await repo.get_job(deps.db, job_id)
    assert row is not None and row.source == "telegram" and row.status is JobStatus.COMPLETED
    assert row.source_ref_hmac == chat_hmac(deps.settings, CHAT_ID)
    assert str(CHAT_ID) not in json.dumps(row.model_dump(mode="json"))  # raw chat id stored nowhere
    ttl = timedelta(hours=deps.settings.telegram_ttl_hours)
    assert row.expires_at is not None and abs((row.expires_at - utcnow()) - ttl) < timedelta(minutes=2)
    kinds = {e.kind: e.content for e in await repo.list_ephemeral(deps.db, job_id)}
    assert kinds == {
        "telegram_url": "https://x.com/someone/status/42",
        "telegram_text": "Forwarded: Cyclone Arnab video",
    }
    entry = await get_entry(deps, job_id)
    assert entry is not None and "Cyclone Arnab video" not in entry.payload_json
    assert "https://x.com/someone/status/42" not in entry.payload_json  # audit holds hashes only (D-14)


async def test_flow_downloads_media_and_help_for_empty(deps: Deps) -> None:
    submitted: list[tuple[VerifyRequest, str | None]] = []

    async def submit(request: VerifyRequest, upload_path: str | None = None, **kw: Any) -> str:
        submitted.append((request, upload_path))
        await repo.create_job(
            deps.db,
            kw["job_id"],
            request=request,
            source="telegram",
            expires_at=kw["expires_at"],
            source_ref_hmac=kw["source_ref_hmac"],
        )
        return str(kw["job_id"])

    async def wait(job_id: str, timeout: float) -> Any:  # noqa: ASYNC109 - mirrors JobRunner.wait
        return make_view(VerdictLabel.UNVERIFIABLE, job_id=job_id)

    async def download(dest: Path) -> None:
        dest.write_bytes(b"\xff\xd8fake-jpeg")  # noqa: ASYNC240

    flow = TelegramFlow(deps, submit, wait)
    out = FakeReplier()
    assert await flow.handle(IncomingMessage(chat_id=1), out) is None
    assert "Privacy" in out.replies[0] and not submitted

    out = FakeReplier()
    job_id = await flow.handle(
        IncomingMessage(chat_id=1, media_kind="photo", media_ext=".jpg", download=download), out
    )
    request, upload = submitted[0]
    assert job_id and upload and Path(upload).read_bytes().startswith(b"\xff\xd8")  # noqa: ASYNC240
    assert request.upload_id == job_id and request.source == "telegram"
    assert Path(upload).is_relative_to(deps.settings.home / "media" / job_id)
    assert "UNVERIFIABLE" in out.edits[-1]
    assert [e.kind for e in await repo.list_ephemeral(deps.db, job_id)] == ["upload_path"]


async def test_ttl_purge_deletes_content_and_redacts(deps: Deps) -> None:
    job_id = new_job_id()
    past = utcnow() - timedelta(hours=1)
    request = VerifyRequest(text="private forwarded message", source="telegram")
    await repo.create_job(
        deps.db,
        job_id,
        request=request,
        source="telegram",
        expires_at=past,
        source_ref_hmac=chat_hmac(deps.settings, CHAT_ID),
    )
    view = make_view(VerdictLabel.FALSE, job_id=job_id, source="telegram")
    await repo.update_job(
        deps.db,
        job_id,
        status=JobStatus.COMPLETED,
        result=view_json(view),
        state={"request": request.model_dump(mode="json")},
    )
    upload = deps.settings.media_dir(job_id) / "upload.jpg"
    upload.write_bytes(b"jpeg")
    await repo.put_ephemeral(
        deps.db, kind="telegram_text", content="private forwarded message", ttl_s=-60, job_id=job_id
    )
    await repo.put_ephemeral(deps.db, kind="upload_path", content=str(upload), ttl_s=-60, job_id=job_id)
    keep_job = new_job_id()
    await repo.create_job(
        deps.db, keep_job, request=request, source="telegram", expires_at=utcnow() + timedelta(hours=5)
    )
    await repo.put_ephemeral(
        deps.db, kind="telegram_text", content="still fresh", ttl_s=3600, job_id=keep_job
    )
    outside = deps.settings.home.parent / "outside.txt"
    outside.write_text("must survive")
    await repo.put_ephemeral(deps.db, kind="upload_path", content=str(outside), ttl_s=-60, job_id=keep_job)

    purged = await purge_expired(deps)

    assert purged >= 4  # 3 ephemeral rows + 1 job
    assert not upload.exists() and not (deps.settings.home / "media" / job_id).exists()
    assert outside.exists()  # never deletes outside VERILOOP_HOME
    row = await repo.get_job(deps.db, job_id)
    assert row is not None and row.redacted and row.request is None and row.state is None
    assert row.result is not None and row.result["claim"] is None and row.result["input"]["url"] is None
    assert row.result["redacted"] is True and row.result["verdict"] == "FALSE" and row.result["sources"]
    assert "private forwarded message" not in json.dumps(row.model_dump(mode="json"))
    fresh = await repo.get_job(deps.db, keep_job)
    assert fresh is not None and not fresh.redacted and fresh.request is not None
    assert [e.content for e in await repo.list_ephemeral(deps.db, keep_job)] == ["still fresh"]
    assert await purge_expired(deps) == 0  # idempotent


async def test_start_telegram_without_token_is_idle(deps: Deps) -> None:
    async def submit(*a: Any, **k: Any) -> str:
        raise AssertionError("must not be called")

    assert await start_telegram(deps, submit) is None
