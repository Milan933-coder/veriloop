"""POST /verify (JSON + multipart), GET /verify/{id}, SSE, middleware, problem+json, auth, rate limit."""

from __future__ import annotations

import asyncio
import re

import pytest

from tests.api.conftest import assert_meta, assert_problem, parse_sse
from tests.orchestrator.builders import FakeServices
from veriloop.contracts import AbstainReason, JobStatus, JobView, VerdictLabel
from veriloop.core.ids import new_job_id
from veriloop.db import repo

FRONTEND_JSON = {
    "text": "This video shows Cyclone Arnab hitting Visakhapatnam.",
    "locale": "en-IN",
    "source": "web",
}


async def test_health_carries_meta_and_echoes_request_id(api) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:
        resp = await h.client.get("/health", headers={"X-Request-Id": "demo-req-1"})
        assert resp.status_code == 200
        body = assert_meta(resp)
        assert body["request_id"] == "demo-req-1"
        assert body["status"] == "ok" and body["db"] == "ok"
        assert body["resolved_models"] == {"llm_primary": "fake-model"}
        assert "gnews_rss" in body["tool_status"] and body["llm_provider"] == "fake"
        bad = await h.client.get("/health", headers={"X-Request-Id": "bad id with spaces\n"})
        assert assert_meta(bad)["request_id"] != "bad id with spaces"


async def test_json_verify_end_to_end_serialises_every_jobview_field(api) -> None:  # type: ignore[no-untyped-def]
    async with api(fakes=FakeServices(label=VerdictLabel.FALSE)) as h:
        resp = await h.client.post("/verify", json=FRONTEND_JSON)
        assert resp.status_code == 202
        accepted = assert_meta(resp)
        job_id = accepted["job_id"]
        assert accepted["status"] == "queued" and accepted["queue_position"] == 0
        assert accepted["status_url"] == f"/verify/{job_id}" == resp.headers["location"]
        assert accepted["events_url"] == f"/verify/{job_id}/events"
        await h.finish(job_id)

        resp = await h.client.get(f"/verify/{job_id}")
        body = assert_meta(resp)
        assert set(JobView.model_fields) <= set(body), set(JobView.model_fields) - set(body)
        assert body["status"] == "completed" and body["verdict"] == "FALSE"
        assert body["audit_id"] is not None and body["report_eligible"] is True
        assert body["legal_eligible"] is False and "MANIPULATED" in body["legal_block_reason"]
        assert [s["id"] for s in body["sources"]][:2] == ["E1", "E2"]
        assert {t["node"] for t in body["trace"]} >= {"resolve_input", "evidence_branch", "audit_write"}
        assert body["input"]["media_hashes"] == [] and body["claim"]["claims"][0]["text"]
        assert body["fusion"]["signals"] and body["debate"]["judge"]["label"] == "FALSE"
        assert body["resolved_models"] == {"llm_primary": "fake-model"} and body["mode"] == "live"
        assert body["queue_position"] is None and body["redacted"] is False and body["error"] is None


async def test_node_exception_via_api_is_audited_tool_outage(api) -> None:  # type: ignore[no-untyped-def]
    async with api(fakes=FakeServices(fail={"gather_evidence": RuntimeError("feed exploded")})) as h:
        job_id = await h.completed_job(FRONTEND_JSON)
        body = (await h.client.get(f"/verify/{job_id}")).json()
        assert body["status"] == "completed"
        assert body["verdict"] == "UNVERIFIABLE" and body["abstain_reason"] == AbstainReason.TOOL_OUTAGE.value
        assert any(e["tool"] == "node:evidence_branch" for e in body["tool_errors"])
        assert body["audit_id"] is not None
        audit = await h.client.get(f"/audit/{job_id}")
        assert audit.status_code == 200 and '"TOOL_OUTAGE"' in audit.json()["payload_json"]


async def test_sse_live_stream_then_replay_and_last_event_id(api) -> None:  # type: ignore[no-untyped-def]
    async with api(fakes=FakeServices(delay=0.05)) as h:
        job_id = await h.submit(FRONTEND_JSON)
        live = await h.client.get(f"/verify/{job_id}/events")  # returns when the job has finished
        assert live.status_code == 200 and live.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(live.text)
        assert events[-1]["type"] == "done" and events[-1]["status"] == "completed"
        nodes = [e for e in events if e.get("type") == "node"]
        assert {"start", "end"} <= {e["status"] for e in nodes}
        assert all("duration_ms" in e for e in nodes if e["status"] == "end")
        statuses = [e for e in events if e.get("type") == "status"]
        assert statuses[0]["status"] == "queued" and "queue_position" in statuses[0]
        assert any(e["status"] == "processing" and "status_detail" in e for e in statuses)

        replay = parse_sse((await h.client.get(f"/verify/{job_id}/events")).text)
        assert [e["seq"] for e in replay] == [e["seq"] for e in events]
        cut = events[3]["seq"]
        tail = parse_sse(
            (await h.client.get(f"/verify/{job_id}/events", headers={"Last-Event-ID": str(cut)})).text
        )
        assert tail[0]["seq"] == cut + 1 and tail[-1]["type"] == "done"


async def test_sse_for_job_finished_by_an_earlier_process(api, deps) -> None:  # type: ignore[no-untyped-def]
    job_id = new_job_id()
    await repo.create_job(deps.db, job_id, request={"text": "x"}, source="web")
    await repo.update_job(deps.db, job_id, status=JobStatus.FAILED, error="interrupted by server restart")
    async with api() as h:
        events = parse_sse((await h.client.get(f"/verify/{job_id}/events")).text)
        assert [e["type"] for e in events] == ["status", "done"]
        assert events[-1]["status"] == "failed" and events[-1]["error"] == "interrupted by server restart"
        view = assert_meta(await h.client.get(f"/verify/{job_id}"))
        assert view["status"] == "failed" and view["error"] and set(JobView.model_fields) <= set(view)


async def test_unknown_job_and_bad_ids_are_404_problems(api) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:
        missing = new_job_id()
        body = assert_problem(await h.client.get(f"/verify/{missing}"), 404)
        assert body["instance"] == f"/verify/{missing}" and body["type"].endswith("job-not-found")
        assert_problem(await h.client.get("/verify/not-a-job"), 404)
        assert_problem(await h.client.get(f"/verify/{missing}/events"), 404)
        assert_problem(await h.client.post(f"/report/{missing}"), 404)
        assert_problem(await h.client.get("/nope-api/x.js"), 404)
        assert_problem(await h.client.delete("/verify"), 405)


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"json": {}}, 422),
        ({"json": {"text": "x", "source": "telegram"}}, 422),
        ({"json": {"url": "ftp://example.org/a"}}, 422),
        ({"json": {"text": "x", "upload_id": "abc"}}, 422),
        ({"json": {"text": "x", "colour": "red"}}, 422),
        ({"json": {"text": "x", "platform": "myspace"}}, 422),
        ({"json": ["text"]}, 422),
        ({"content": b"{not json", "headers": {"content-type": "application/json"}}, 400),
        ({"content": b"hello", "headers": {"content-type": "text/plain"}}, 415),
    ],
)
async def test_verify_rejects_bad_bodies_with_problem_json(api, kwargs, status) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:
        body = assert_problem(await h.client.post("/verify", **kwargs), status)
        if status == 422 and "errors" in body:
            assert isinstance(body["errors"], list)


async def test_multipart_upload_is_saved_under_media_dir(api, deps) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:
        files = {"file": ("photo.PNG", b"\x89PNG\r\n\x1a\nfake-image-bytes", "image/png")}
        data = {"locale": "hi-IN", "platform": "upload", "source": "pwa", "text": "Is this real?"}
        resp = await h.client.post("/verify", files=files, data=data)
        assert resp.status_code == 202, resp.text
        job_id = assert_meta(resp)["job_id"]
        saved = deps.settings.home / "media" / job_id / "upload.png"
        assert saved.read_bytes().startswith(b"\x89PNG")
        row = await repo.get_job(deps.db, job_id)
        assert row is not None and row.request is not None
        assert row.request["upload_id"] == job_id and row.request["platform"] == "upload"
        assert row.request["locale"] == "hi-IN" and row.request["source"] == "pwa"
        await h.finish(job_id)

        bad = await h.client.post("/verify", files={"file": ("notes.txt", b"hello", "text/plain")}, data=data)
        assert_problem(bad, 415)
        assert_problem(await h.client.post("/verify", files={"file": ("e.png", b"", "image/png")}), 422)
        assert_problem(
            await h.client.post("/verify", data={"text": "no file"}, files={"x": ("a", b"1")}), 422
        )


async def test_upload_over_the_limit_is_413_and_leaves_nothing(api, deps) -> None:  # type: ignore[no-untyped-def]
    async with api(max_upload_mb=1) as h:
        before = (
            set((deps.settings.home / "media").glob("*"))
            if (deps.settings.home / "media").exists()
            else set()
        )
        big = b"\x00" * (1024 * 1024 + 10)
        resp = await h.client.post("/verify", files={"file": ("v.mp4", big, "video/mp4")})
        assert_problem(resp, 413)
        after = set((deps.settings.home / "media").glob("*"))
        assert after == before


async def test_api_key_auth_with_same_origin_exemption(api) -> None:  # type: ignore[no-untyped-def]
    async with api(api_keys="k-one, k-two") as h:
        assert_problem(await h.client.post("/verify", json=FRONTEND_JSON), 401)
        assert_problem(
            await h.client.post("/verify", json=FRONTEND_JSON, headers={"X-API-Key": "wrong"}), 401
        )
        assert (
            await h.client.post("/verify", json=FRONTEND_JSON, headers={"X-API-Key": "k-two"})
        ).status_code == 202
        same_site = {"Sec-Fetch-Site": "same-origin"}
        job_id = await h.submit(FRONTEND_JSON, headers=same_site)
        await h.finish(job_id)
        assert (
            await h.client.get(f"/verify/{job_id}", headers={"Origin": "http://testserver"})
        ).status_code == 200
        assert (await h.client.get(f"/verify/{job_id}/events", headers=same_site)).status_code == 200
        assert_problem(
            await h.client.get(f"/verify/{job_id}", headers={"Origin": "https://evil.example"}), 401
        )
        # public by design: health, audit records (QR codes), public key
        assert (await h.client.get("/health")).status_code == 200
        assert (await h.client.get(f"/audit/{job_id}")).status_code == 200
        assert (await h.client.get("/audit/pubkey")).status_code == 200


async def test_rate_limit_on_post_verify_is_429_problem(api) -> None:  # type: ignore[no-untyped-def]
    async with api(rate_limit_per_min=2) as h:
        for _ in range(2):
            assert (await h.client.post("/verify", json=FRONTEND_JSON)).status_code == 202
        resp = await h.client.post("/verify", json=FRONTEND_JSON)
        body = assert_problem(resp, 429)
        assert resp.headers["retry-after"] == "60" and body["type"].endswith("rate-limited")
        assert (await h.client.get("/health")).status_code == 200  # only POST /verify is limited


async def test_rate_limited_tool_shows_in_status_detail(api, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async with api(fakes=FakeServices(delay=0.3)) as h:
        monkeypatch.setattr(h.runtime.deps.tools, "rate_limited_tools", lambda *a, **k: ["gdelt"])
        job_id = await h.submit(FRONTEND_JSON)
        for _ in range(50):
            body = (await h.client.get(f"/verify/{job_id}")).json()
            if body["status"] == "processing":
                break
            await asyncio.sleep(0.02)
        assert body["status"] == "processing"
        assert re.search(r"rate-limited: gdelt", body["status_detail"])
        await h.finish(job_id)


async def test_admin_chaos_requires_admin_key(api) -> None:  # type: ignore[no-untyped-def]
    async with api(admin_key="s3cret") as h:
        assert_problem(await h.client.post("/admin/chaos", json={"tool": "gdelt", "mode": "timeout"}), 401)
        wrong = {"X-Admin-Key": "nope"}
        assert_problem(
            await h.client.post("/admin/chaos", json={"tool": "gdelt", "mode": "error"}, headers=wrong), 401
        )
        key = {"X-Admin-Key": "s3cret"}
        resp = await h.client.post("/admin/chaos", json={"tool": "gdelt", "mode": "timeout"}, headers=key)
        assert assert_meta(resp)["active"] == {"gdelt": "timeout"}
        assert h.runtime.deps.tools.chaos.get("gdelt") == "timeout"
        assert (await h.client.get("/admin/chaos", headers=key)).json()["active"] == {"gdelt": "timeout"}
        health = (await h.client.get("/health")).json()
        assert health["tool_status"]["gdelt"]["state"] == "chaos"
        assert_problem(
            await h.client.post("/admin/chaos", json={"tool": "nope", "mode": "off"}, headers=key), 422
        )
        assert_problem(
            await h.client.post("/admin/chaos", json={"tool": "gdelt", "mode": "melt"}, headers=key), 422
        )
        off = await h.client.post("/admin/chaos", json={"tool": "gdelt", "mode": "off"}, headers=key)
        assert off.json()["active"] == {}


async def test_unhandled_error_is_500_problem_json(api, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:

        async def boom(job_id: str) -> None:
            raise RuntimeError("database fell over")

        monkeypatch.setattr(h.runtime.runner, "get_view", boom)
        body = assert_problem(await h.client.get(f"/verify/{new_job_id()}"), 500)
        assert "RuntimeError" in body["detail"] and "database fell over" not in body["detail"]


async def test_cors_preflight_for_vite_dev_server(api) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:
        resp = await h.client.options(
            "/verify",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "x-request-id" in resp.headers
        other = await h.client.options(
            "/verify", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
        )
        assert "access-control-allow-origin" not in other.headers


async def test_telegram_webhook_is_404_when_bot_is_not_running(api) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:
        assert_problem(await h.client.post("/telegram/webhook", json={"update_id": 1}), 404)
