"""Report / legal / audit endpoints over HTTP (G5 human gate, D4 never send, browser-side audit check)."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from tests.api.conftest import assert_meta, assert_problem
from tests.orchestrator.builders import FakeServices
from veriloop.contracts import ForensicStatus, VerdictLabel
from veriloop.core.hashing import sha256_hex
from veriloop.core.ids import new_job_id
from veriloop.db import repo

TEXT = {"text": "This video shows Cyclone Arnab hitting Visakhapatnam.", "locale": "en-IN", "source": "web"}
URL = {"url": "https://x.com/someone/status/123", "locale": "en-IN", "source": "web"}
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ACKS = {
    "is_subject_or_authorized": True,
    "not_legal_advice": True,
    "no_auto_send": True,
    "will_review_before_filing": True,
}


async def test_report_prepare_confirm_card_bundle(api, deps) -> None:  # type: ignore[no-untyped-def]
    async with api(fakes=FakeServices(label=VerdictLabel.FALSE)) as h:
        job_id = await h.completed_job(URL)

        pkg = assert_meta(await h.client.post(f"/report/{job_id}"))
        assert pkg["eligible"] is True and pkg["ineligible_reason"] is None
        assert "Verdict: FALSE" in pkg["preview_text"] and pkg["audit_id"] is not None
        assert pkg["bundle"]["pdf_url"].endswith(f"/audit/{job_id}/pdf")
        assert pkg["bundle"]["verify_url"].endswith(f"/audit/{job_id}/verify")
        assert pkg["bundle_url"].endswith(f"/report/{job_id}/bundle.zip")
        methods = {p["platform"]: p["method"] for p in pkg["platform_plans"]}
        assert methods == {"x": "playwright_guided", "telegram": "report_card", "whatsapp": "report_card"}
        assert all(p["card_url"].endswith(f"/report/{job_id}/card.png") for p in pkg["platform_plans"])

        no = await h.client.post(f"/report/{job_id}/confirm", json={"platform": "x", "confirmed": False})
        assert_problem(no, 422)
        assert_problem(await h.client.post(f"/report/{job_id}/confirm", json={"platform": "x"}), 422)
        assert_problem(
            await h.client.post(f"/report/{job_id}/confirm", json={"platform": "myspace", "confirmed": True}),
            422,
        )
        resp = await h.client.post(f"/report/{job_id}/confirm", json={"platform": "x", "confirmed": True})
        confirmed = assert_meta(resp)
        assert confirmed["attempt_logged"] is True and confirmed["method"] == "playwright_guided"
        assert f"--job {job_id}" in confirmed["reporter_agent_command"] and confirmed["audit_id"]
        attempts = await repo.list_report_attempts(deps.db, job_id)
        assert [a.attempt_id for a in attempts] == [confirmed["attempt_id"]]

        plan = assert_meta(await h.client.get(f"/report/{job_id}/attempts/{confirmed['attempt_id']}"))
        assert plan["platform"] == "x" and plan["plan"]["prefilled_text"] and plan["status"] == "confirmed"
        assert_problem(await h.client.get(f"/report/{job_id}/attempts/ra_missing"), 404)

        card = await h.client.get(f"/report/{job_id}/card.png")
        assert card.status_code == 200 and card.headers["content-type"] == "image/png"
        assert card.content.startswith(b"\x89PNG") and "x-request-id" in card.headers

        bundle = await h.client.get(f"/report/{job_id}/bundle.zip")
        assert bundle.status_code == 200 and bundle.headers["content-type"] == "application/zip"
        assert "attachment" in bundle.headers["content-disposition"]
        names = set(zipfile.ZipFile(io.BytesIO(bundle.content)).namelist())
        assert {
            "verdict.json",
            "sources.json",
            "forensic_report.json",
            "media_hashes.json",
            "audit_record.pdf",
            "report_card.png",
            "bundle.json",
        } <= names

        events = [json.loads(e.payload_json)["event_type"] for e in await _entries(h, job_id)]
        assert events == ["verdict", "report_prepared", "report_confirmed"]
        assert (await h.client.get(f"/audit/{job_id}/verify")).json()["ok"] is True


def _load_script(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _entries(h, job_id: str):  # type: ignore[no-untyped-def]
    from veriloop.services.audit import get_entries

    return await get_entries(h.runtime.deps, job_id)


async def test_report_is_refused_for_true_and_unfinished_jobs(api, deps) -> None:  # type: ignore[no-untyped-def]
    async with api(fakes=FakeServices(label=VerdictLabel.TRUE)) as h:
        job_id = await h.completed_job(TEXT)
        pkg = assert_meta(await h.client.post(f"/report/{job_id}"))
        assert pkg["eligible"] is False and "TRUE" in pkg["ineligible_reason"] and pkg["platform_plans"] == []
        refused = await h.client.post(f"/report/{job_id}/confirm", json={"platform": "x", "confirmed": True})
        assert_problem(refused, 409)

        queued = new_job_id()
        await repo.create_job(deps.db, queued, request=TEXT)
        assert_problem(await h.client.get(f"/report/{queued}/card.png"), 409)
        assert_problem(await h.client.get(f"/report/{queued}/bundle.zip"), 409)


async def test_legal_draft_gate_and_draft(api, deps) -> None:  # type: ignore[no-untyped-def]
    fakes = FakeServices(
        label=VerdictLabel.MANIPULATED,
        identifiable=True,
        private=False,
        forensic_status=ForensicStatus.MANIPULATED,
        groups_manipulation=2,
    )
    async with api(fakes=fakes) as h:
        job_id = await h.completed_job(URL)
        view = (await h.client.get(f"/verify/{job_id}")).json()
        assert view["verdict"] == "MANIPULATED" and view["legal_eligible"] is True

        missing = await h.client.post(
            f"/legal/{job_id}/draft",
            json={"jurisdiction": "IN", "acknowledgements": {**ACKS, "no_auto_send": False}},
        )
        body = assert_problem(missing, 422)
        assert body["missing_acknowledgements"] == ["no_auto_send"]
        assert_problem(await h.client.post(f"/legal/{job_id}/draft", json={"jurisdiction": "IN"}), 422)
        assert_problem(
            await h.client.post(
                f"/legal/{job_id}/draft", json={"jurisdiction": "FR", "acknowledgements": ACKS}
            ),
            422,
        )

        resp = await h.client.post(
            f"/legal/{job_id}/draft", json={"jurisdiction": "IN", "acknowledgements": ACKS, "platform": None}
        )
        draft = assert_meta(resp)
        assert "This is a draft, not legal advice. VeriLoop never sends it." in draft["draft_text"]
        assert draft["placeholders"] and draft["checklist"] and draft["jurisdiction"] == "IN"
        assert job_id in draft["evidence_bundle_ref"] and draft["audit_id"]
        stored = await repo.list_legal_drafts(deps.db, job_id)
        assert len(stored) == 1 and stored[0].draft_sha256 == sha256_hex(draft["draft_text"])
        assert draft["draft_text"] not in json.dumps([s.model_dump(mode="json") for s in stored])

    async with api(fakes=FakeServices(label=VerdictLabel.FALSE)) as h:
        job_id = await h.completed_job(URL)
        refused = await h.client.post(
            f"/legal/{job_id}/draft", json={"jurisdiction": "EU", "acknowledgements": ACKS}
        )
        assert_problem(refused, 409)


async def test_audit_entry_matches_the_browser_side_check(api) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:
        job_id = await h.completed_job(TEXT)
        entry = assert_meta(await h.client.get(f"/audit/{job_id}"))
        # exactly what frontend/src/lib/auditCrypto.ts does
        assert sha256_hex(entry["payload_json"].encode("utf-8")) == entry["payload_hash"]
        assert entry["payload_hash"] == entry["payload_hash"].lower() and len(entry["payload_hash"]) == 64
        pem = await h.client.get("/audit/pubkey")
        assert pem.status_code == 200 and pem.headers["content-type"].startswith("text/plain")
        assert pem.text.startswith("-----BEGIN PUBLIC KEY-----")
        key = load_pem_public_key(pem.text.encode())
        assert isinstance(key, Ed25519PublicKey)
        key.verify(
            base64.b64decode(entry["signature"]), f"{entry['prev_hash']}:{entry['payload_hash']}".encode()
        )
        payload = json.loads(entry["payload_json"])
        assert payload["resolved_model_ids"] == entry["resolved_model_ids"] == {"llm_primary": "fake-model"}
        assert payload["tool_versions"] == entry["tool_versions"] and "gnews_rss" in entry["tool_versions"]

        pdf = await h.client.get(f"/audit/{job_id}/pdf")
        assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")

        result = assert_meta(await h.client.get(f"/audit/{job_id}/verify"))
        assert result["ok"] is True and result["broken_at"] is None and entry["id"] in result["job_entry_ids"]

        missing = new_job_id()
        assert_problem(await h.client.get(f"/audit/{missing}"), 404)
        assert_problem(await h.client.get(f"/audit/{missing}/pdf"), 404)
        assert_problem(await h.client.get(f"/audit/{missing}/verify"), 404)


async def test_tamper_demo_shows_broken_at_then_restore(api, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    tamper = _load_script("tamper_demo").tamper
    restore = _load_script("restore_chain").restore

    async with api() as h:
        job_id = await h.completed_job(TEXT)
        entry = (await h.client.get(f"/audit/{job_id}")).json()
        backup = tmp_path / "backup.json"
        info = await tamper(h.runtime.deps.db, backup, job_id=job_id)
        broken = assert_meta(await h.client.get(f"/audit/{job_id}/verify"))
        assert broken["ok"] is False and broken["broken_at"] == info["row_id"] == entry["id"]
        assert "payload_hash" in broken["reason"]
        await restore(h.runtime.deps.db, backup)
        assert (await h.client.get(f"/audit/{job_id}/verify")).json()["ok"] is True


async def test_telegram_job_audit_row_holds_only_hashes(api) -> None:  # type: ignore[no-untyped-def]
    from veriloop.contracts import VerifyRequest

    secret = "Forwarded: secret family message about Cyclone Arnab in Visakhapatnam"
    async with api() as h:
        # the HTTP API refuses source=telegram; the bot submits through the runner like this
        job_id = await h.runtime.runner.submit(
            VerifyRequest(text=secret, url="https://t.me/c/123/456", source="telegram")
        )
        await h.finish(job_id)
        entry = assert_meta(await h.client.get(f"/audit/{job_id}"))
        assert secret not in entry["payload_json"] and "t.me/c/123/456" not in entry["payload_json"]
        payload = json.loads(entry["payload_json"])
        assert payload["content_policy"] == "sha256_only" and payload["source"] == "telegram"
        assert payload["input"]["text_sha256"] == sha256_hex(secret)
        assert all(t["detail"] is None for t in payload["trace"])
        assert (await h.client.get(f"/verify/{job_id}")).json()["source"] == "telegram"
        # D7: the job row keeps only hashes of the message; the raw state is not persisted at all
        row = await repo.get_job(h.runtime.deps.db, job_id)
        assert row is not None and row.state is None and row.expires_at is None
        assert secret not in json.dumps(row.request) and row.request["text_sha256"] == sha256_hex(secret)
