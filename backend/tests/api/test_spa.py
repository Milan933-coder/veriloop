"""SPA serving (frontend/dist), fallback routing, API precedence, and small app helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from tests.api.conftest import assert_meta, assert_problem
from veriloop.api.app import keep_temp_files_on_home
from veriloop.api.context import inject_meta
from veriloop.api.intake import upload_extension
from veriloop.api.spa import cache_control, safe_file

INDEX = "<!doctype html><html><head><title>VeriLoop</title></head><body><div id=root></div></body></html>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "icons").mkdir()
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log('vl')", encoding="utf-8")
    (root / "assets" / "index-abc123.css").write_text("body{}", encoding="utf-8")
    (root / "icons" / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "manifest.webmanifest").write_text('{"name":"VeriLoop"}', encoding="utf-8")
    (root / "sw.js").write_text("self.addEventListener('fetch',()=>{})", encoding="utf-8")
    (root / "registerSW.js").write_text("navigator.serviceWorker", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside dist", encoding="utf-8")
    return root


async def test_spa_files_fallback_and_api_precedence(api, dist: Path) -> None:  # type: ignore[no-untyped-def]
    async with api(dist=dist) as h:
        for path in ("/", "/j/0123456789abcdef", "/a/0123456789abcdef", "/share?text=hi", "/about"):
            resp = await h.client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers["content-type"].startswith("text/html") and "<div id=root>" in resp.text
            assert resp.headers["cache-control"] == "no-cache"

        js = await h.client.get("/assets/index-abc123.js")
        assert js.headers["content-type"].startswith("text/javascript")
        assert "immutable" in js.headers["cache-control"]
        assert (await h.client.get("/assets/index-abc123.css")).headers["content-type"].startswith("text/css")
        manifest = await h.client.get("/manifest.webmanifest")
        assert manifest.headers["content-type"] == "application/manifest+json"
        assert manifest.headers["cache-control"] == "no-cache"
        sw = await h.client.get("/sw.js")
        assert sw.headers["cache-control"] == "no-cache" and sw.headers["content-type"].startswith(
            "text/javascript"
        )
        assert (await h.client.get("/registerSW.js")).headers["cache-control"] == "no-cache"
        assert (await h.client.get("/icons/icon-192.png")).headers["content-type"] == "image/png"

        assert_problem(await h.client.get("/assets/missing.js"), 404)
        assert_problem(await h.client.get("/icons/nope"), 404)
        assert_problem(await h.client.get("/../secret.txt"), 404)
        assert_problem(await h.client.get("/verify"), 404)  # API namespace never falls back to HTML
        assert_problem(await h.client.get("/audit/0123456789abcdef/extra/path"), 404)
        health = await h.client.get("/health")
        assert health.headers["content-type"] == "application/json" and assert_meta(health)["status"] == "ok"


async def test_without_dist_root_returns_json_hint(api, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    async with api(dist=tmp_path / "missing-dist") as h:
        body = assert_meta(await h.client.get("/"))
        assert "npm run build" in body["hint"]
        assert_problem(await h.client.get("/j/whatever"), 404)


def test_safe_file_rejects_traversal(dist: Path) -> None:
    assert safe_file(dist, "index.html") == (dist / "index.html").resolve()
    assert safe_file(dist, "../secret.txt") is None
    assert safe_file(dist, "assets/../../secret.txt") is None
    assert safe_file(dist, "assets\\..\\..\\secret.txt") is None
    assert safe_file(dist, "") is None
    assert cache_control("assets/x.js").endswith("immutable") and cache_control("sw.js") == "no-cache"


def test_inject_meta_only_touches_json_objects() -> None:
    assert inject_meta(b'{"a":1}', "rid", 5) == b'{"a":1,"request_id":"rid","latency_ms":5}'
    assert inject_meta(b"[1,2]", "rid", 5) == b"[1,2]"
    assert inject_meta(b"not json", "rid", 5) == b"not json"
    # strings (e.g. payload_json with non-ASCII text) survive the rewrite byte-for-byte
    raw = '{"payload_json":"{\\"t\\":\\"विशाखापत्तनम\\"}"}'.encode()
    out = inject_meta(raw, "r", 1)
    import json

    assert json.loads(out)["payload_json"] == json.loads(raw)["payload_json"]


def test_upload_extension() -> None:
    assert upload_extension("Clip.MP4", "video/mp4") == ".mp4"
    assert upload_extension("weird.name.with.toolongext", "image/jpeg") in (".jpg", ".jpeg", ".jpe")
    assert upload_extension(None, "application/x-unknown") == ".bin"
    assert upload_extension("../../evil.png", "image/png") == ".png"


def test_temp_files_are_redirected_to_home(make_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings()
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    for var in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(var, os.environ.get(var, ""))
    tmp = keep_temp_files_on_home(settings)
    assert tmp == settings.home / "tmp" and tmp.is_dir()
    assert tempfile.gettempdir() == str(tmp) and os.environ["TEMP"] == str(tmp)


async def test_openapi_document_is_not_polluted_with_meta(api) -> None:  # type: ignore[no-untyped-def]
    async with api() as h:
        resp = await h.client.get("/openapi.json")
        doc = resp.json()
        assert "request_id" not in doc and "latency_ms" not in doc and "x-request-id" in resp.headers
        assert {"/verify", "/verify/{job_id}/events", "/audit/pubkey", "/legal/{job_id}/draft"} <= set(
            doc["paths"]
        )
