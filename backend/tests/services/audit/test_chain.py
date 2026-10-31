"""Audit chain: canonical payloads, linking, signatures, tamper detection, concurrency, PDF."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import secrets
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tests.orchestrator.builders import make_view
from veriloop.contracts import AuditEventType, VerdictLabel, VerifyRequest
from veriloop.core.hashing import canonical_json, sha256_hex
from veriloop.db.engine import Database
from veriloop.deps import Deps
from veriloop.services.audit import (
    GENESIS_HASH,
    append_event,
    build_verdict_payload,
    get_entries,
    get_entry,
    public_key_pem,
    render_pdf,
    verify_chain,
)
from veriloop.services.audit.keys import derive_secret, generate_private_pem, load_signing_key

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _load_script(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
async def chain_db(db: Database) -> AsyncIterator[Database]:
    """A database of its own, so chain-wide checks never see other packages' rows."""
    name = f"vl_audit_{secrets.token_hex(4)}"
    dsn = await db.create_database(name)
    fresh = await Database.connect(dsn=dsn, min_size=1, max_size=6)
    await fresh.apply_schema()
    try:
        yield fresh
    finally:
        await fresh.close()
        await db.drop_database(name)


@pytest.fixture
async def chain(chain_db: Database, deps: Deps) -> AsyncIterator[Deps]:
    """Deps on an empty chain that starts at genesis (superuser TRUNCATE between tests)."""
    async with chain_db.transaction() as conn:
        await conn.execute("ALTER TABLE audit_chain DISABLE TRIGGER USER")
        await conn.execute("TRUNCATE audit_chain RESTART IDENTITY")
        await conn.execute("ALTER TABLE audit_chain ENABLE TRIGGER USER")
    original = deps.db
    deps.db = chain_db
    try:
        yield deps
    finally:
        deps.db = original


async def _append_n(deps: Deps, n: int, job_id: str = "job_a") -> list[int]:
    ids = []
    for i in range(n):
        entry = await append_event(
            deps, job_id, AuditEventType.VERDICT, {"i": i, "score": 1 / 3, "note": "हिन्दी"}
        )
        ids.append(entry.id)
    return ids


async def test_rows_are_canonical_linked_and_signed(chain: Deps) -> None:
    await _append_n(chain, 3)
    entries = await get_entries(chain, "job_a")
    assert [e.prev_hash for e in entries] == [GENESIS_HASH, entries[0].payload_hash, entries[1].payload_hash]
    public = serialization.load_pem_public_key(public_key_pem(chain.settings).encode())
    assert isinstance(public, Ed25519PublicKey)
    for e in entries:
        payload = json.loads(e.payload_json)
        assert canonical_json(payload) == e.payload_json  # exact canonical text
        assert (
            hashlib.sha256(e.payload_json.encode("utf-8")).hexdigest() == e.payload_hash
        )  # what the browser checks
        public.verify(base64.b64decode(e.signature), f"{e.prev_hash}:{e.payload_hash}".encode())
        assert payload["resolved_model_ids"] == e.resolved_model_ids == {"llm_primary": "fake-model"}
        assert payload["tool_versions"] == e.tool_versions and "gnews_rss" in e.tool_versions
        assert payload["score"] == 0.333333 and payload["note"] == "हिन्दी"
        der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        assert e.key_fingerprint == hashlib.sha256(der).hexdigest()
    result = await verify_chain(chain, "job_a")
    assert result.ok and result.checked_rows == 3 and result.head_hash == entries[-1].payload_hash
    assert result.job_entry_ids == [e.id for e in entries]


async def test_reserved_fields_cannot_be_spoofed(chain: Deps) -> None:
    entry = await append_event(
        chain,
        "job_b",
        AuditEventType.REPORT_PREPARED,
        {"job_id": "evil", "resolved_model_ids": {"x": "y"}, "ok": True},
    )
    payload = json.loads(entry.payload_json)
    assert payload["job_id"] == "job_b" and payload["event_type"] == "report_prepared"
    assert payload["resolved_model_ids"] == {"llm_primary": "fake-model"}
    assert await get_entry(chain, "job_b") is None  # default event type is VERDICT
    assert (await get_entry(chain, "job_b", AuditEventType.REPORT_PREPARED)) == entry


async def test_tamper_demo_and_restore(chain: Deps, tmp_path: Path) -> None:
    ids = await _append_n(chain, 4)
    tamper_demo = _load_script("tamper_demo")
    restore_chain = _load_script("restore_chain")
    backup = tmp_path / "backup.json"

    info = await tamper_demo.tamper(chain.db, backup, row_id=ids[1])
    assert info["row_id"] == ids[1] and backup.exists()
    broken = await verify_chain(chain, "job_a")
    assert not broken.ok and broken.broken_at == ids[1] and "payload_hash" in (broken.reason or "")
    with pytest.raises(RuntimeError):
        await tamper_demo.tamper(chain.db, backup)  # refuses to tamper twice

    await restore_chain.restore(chain.db, backup)
    assert not backup.exists()
    assert (await verify_chain(chain)).ok

    await tamper_demo.tamper(chain.db, backup, row_id=ids[2], rehash=True)
    broken = await verify_chain(chain)
    assert not broken.ok and broken.broken_at == ids[2] and "signature" in (broken.reason or "")
    await restore_chain.restore(chain.db, backup)
    assert (await verify_chain(chain)).ok


async def test_tamper_targets_latest_verdict_row_by_default(chain: Deps, tmp_path: Path) -> None:
    await _append_n(chain, 2)
    entry = await append_event(
        chain, "job_v", AuditEventType.VERDICT, {"verdict": {"label": "FALSE", "confidence": 0.8}}
    )
    tamper_demo = _load_script("tamper_demo")
    info = await tamper_demo.tamper(chain.db, tmp_path / "b.json", job_id="job_v")
    assert info["row_id"] == entry.id and "FALSE -> TRUE" in info["change"]
    result = await verify_chain(chain, "job_v")
    assert result.broken_at == entry.id and result.job_entry_ids == [entry.id]


async def _as_superuser(db: Database, sql: str, *args: object) -> None:
    async with db.transaction() as conn:
        await conn.execute("ALTER TABLE audit_chain DISABLE TRIGGER USER")
        await conn.execute(sql, *args)
        await conn.execute("ALTER TABLE audit_chain ENABLE TRIGGER USER")


async def test_column_edit_and_deleted_row_are_detected(chain: Deps) -> None:
    ids = await _append_n(chain, 3)
    await _as_superuser(chain.db, "UPDATE audit_chain SET job_id = 'other' WHERE id = $1", ids[0])
    broken = await verify_chain(chain)
    assert broken.broken_at == ids[0] and "job_id" in (broken.reason or "")
    await _as_superuser(chain.db, "UPDATE audit_chain SET job_id = 'job_a' WHERE id = $1", ids[0])
    assert (await verify_chain(chain)).ok

    await _as_superuser(chain.db, "DELETE FROM audit_chain WHERE id = $1", ids[1])
    broken = await verify_chain(chain)
    assert broken.broken_at == ids[2] and "prev_hash" in (broken.reason or "")


async def test_append_only_trigger_blocks_edits(chain: Deps) -> None:
    ids = await _append_n(chain, 1)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await chain.db.execute("UPDATE audit_chain SET payload_json = '{}' WHERE id = $1", ids[0])
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await chain.db.execute("DELETE FROM audit_chain WHERE id = $1", ids[0])


async def test_concurrent_appends_stay_linked(chain: Deps) -> None:
    entries = await asyncio.gather(
        *(append_event(chain, f"job_{i % 5}", AuditEventType.VERDICT, {"i": i}) for i in range(30))
    )
    assert len({e.id for e in entries}) == 30
    result = await verify_chain(chain)
    assert result.ok and result.checked_rows == 30
    rows = await chain.db.fetch("SELECT prev_hash, payload_hash FROM audit_chain ORDER BY id")
    assert all(rows[i]["prev_hash"] == rows[i - 1]["payload_hash"] for i in range(1, len(rows)))


async def test_unknown_signing_key_is_detected(chain: Deps, make_settings) -> None:  # type: ignore[no-untyped-def]
    await _append_n(chain, 2)
    original = chain.settings
    chain.settings = make_settings(audit_ed25519_key=generate_private_pem())
    try:
        result = await verify_chain(chain)
    finally:
        chain.settings = original
    assert not result.ok and result.broken_at is not None and "unknown key" in (result.reason or "")


def test_key_file_is_generated_once_and_reused(make_settings) -> None:  # type: ignore[no-untyped-def]
    settings = make_settings()  # no AUDIT_ED25519_KEY -> file under VERILOOP_HOME/keys
    first = load_signing_key(settings)
    assert first.source == "generated" and (settings.keys_dir / "audit_ed25519.pem").is_file()
    assert load_signing_key(settings) is first  # cached
    assert "BEGIN PUBLIC KEY" in public_key_pem(settings)
    one_line = generate_private_pem().replace("\n", "\\n")  # .env style
    assert load_signing_key(make_settings(audit_ed25519_key=one_line)).source == "env"
    a, b = derive_secret(settings, "telegram-chat-hmac"), derive_secret(settings, "other")
    assert a != b and a == derive_secret(settings, "telegram-chat-hmac") and len(a) == 32


async def test_pdf_renders_with_payload_and_unicode(chain: Deps) -> None:
    view = make_view(VerdictLabel.FALSE, job_id="job_pdf")
    view = view.model_copy(update={"claim": view.claim.model_copy(update={"caption_text": "केंद्रीय मंत्रिमंडल"})})
    payload = build_verdict_payload(view, VerifyRequest(text="केंद्रीय मंत्रिमंडल ने 3 प्रतिशत"), chain.settings)
    entry = await append_event(chain, "job_pdf", AuditEventType.VERDICT, payload)
    pdf = render_pdf(entry, view, chain.settings)
    assert pdf.startswith(b"%PDF") and len(pdf) > 5000 and b"/Type /Page" in pdf
    report = await append_event(chain, "job_pdf", AuditEventType.REPORT_CONFIRMED, {"platform": "x"})
    assert render_pdf(report, None, chain.settings).startswith(b"%PDF")


def test_telegram_payload_stores_only_hashes(settings) -> None:  # type: ignore[no-untyped-def]
    view = make_view(VerdictLabel.FALSE, source="telegram", url="https://t.me/c/1/2")
    text = "Secret forwarded message about Visakhapatnam"
    payload = build_verdict_payload(
        view, VerifyRequest(text=text, url=view.input.url, source="telegram"), settings
    )
    blob = canonical_json(payload)
    assert text not in blob and "https://t.me/c/1/2" not in blob
    assert view.claim.claims[0].text not in blob and view.rationale[0].text not in blob
    assert payload["input"]["text_sha256"] == sha256_hex(text)
    assert payload["input"]["url_sha256"] == sha256_hex("https://t.me/c/1/2")
    assert payload["content_policy"] == "sha256_only"
    assert payload["evidence"][0]["url"].startswith("https://newschecker.in")  # tool evidence stays
    # stage text that can echo user input (node errors, fetch labels) is hashed too
    leaky = view.model_copy(
        update={
            "abstain_detail": "Pipeline stage(s) failed: resolve_input (could not fetch https://t.me/c/1/2)",
            "labels": [*settings.mode_labels(), "Could not fetch https://t.me/c/1/2 - caption only"],
        }
    )
    private = canonical_json(build_verdict_payload(leaky, None, settings))
    assert "https://t.me/c/1/2" not in private
    hashed = build_verdict_payload(leaky, None, settings)
    assert hashed["verdict"]["abstain_detail_sha256"] == sha256_hex(leaky.abstain_detail)
    assert hashed["labels"] == settings.mode_labels() and len(hashed["labels_sha256"]) == 1
    web = build_verdict_payload(make_view(), VerifyRequest(text=text), settings)
    assert web["claim"]["text"] and web["input"]["url"] and web["weights"] == {"news_stance": 0.25}
    assert web["thresholds"]["abstain_threshold"] == 0.7 and web["calibration_version"] == "cal-test"
