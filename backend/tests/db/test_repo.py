from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from veriloop.contracts import (
    EvidenceItem,
    JobStatus,
    LegalAcknowledgements,
    MediaAsset,
    MediaKind,
    ModelScore,
    RetrievalMethod,
    Stance,
)
from veriloop.core.cache import ToolCache
from veriloop.core.hashing import sha256_hex
from veriloop.core.ids import new_id, new_job_id
from veriloop.core.testing import FakeEmbedder
from veriloop.db import repo
from veriloop.db.engine import Database

UTC = UTC


async def new_job(db: Database, **kw) -> str:
    job_id = new_job_id()
    await repo.create_job(db, job_id, request={"text": "claim"}, **kw)
    return job_id


def evidence(i: int, text: str) -> EvidenceItem:
    return EvidenceItem(
        id=f"E{i}",
        source_name="Reuters",
        url=f"https://www.reuters.com/a{i}",
        domain="reuters.com",
        publisher_group="reuters",
        title=f"t{i}",
        snippet=text,
        retrieval_method=RetrievalMethod.GOOGLE_NEWS_RSS,
        tool="gnews_rss",
        stance=Stance.SUPPORT,
        published_at=datetime(2026, 10, 30, tzinfo=UTC),
    )


def unit(vec: np.ndarray) -> np.ndarray:
    return (vec / np.linalg.norm(vec)).astype(np.float32)


# ---- jobs ------------------------------------------------------------------------------------
async def test_job_crud_state_merge_and_queue_position(db: Database) -> None:
    a = await new_job(db, source="telegram", source_ref_hmac="hmac123")
    b = await new_job(db)
    row = await repo.get_job(db, a)
    assert (
        row
        and row.status is JobStatus.QUEUED
        and row.request == {"text": "claim"}
        and row.source == "telegram"
    )
    assert await repo.queue_position(db, b) >= 1 + (await repo.queue_position(db, a))

    assert await repo.update_job(db, a, status=JobStatus.PROCESSING, started_at=datetime.now(UTC))
    assert await repo.merge_job_state(db, a, {"node": "extract", "n": 1})
    assert await repo.merge_job_state(db, a, {"n": 2})
    assert await repo.update_job(
        db, a, status="completed", result={"verdict": "FALSE"}, verdict="FALSE", confidence=0.83, audit_id=7
    )
    row = await repo.get_job(db, a)
    assert row.status is JobStatus.COMPLETED and row.state == {"node": "extract", "n": 2}
    assert row.result == {"verdict": "FALSE"} and row.confidence == 0.83 and row.audit_id == 7
    assert row.updated_at >= row.created_at

    assert await repo.update_job(db, a, request=None, redacted=True)
    row = await repo.get_job(db, a)
    assert row.request is None and row.redacted
    assert not await repo.update_job(db, "missing-job", status="failed")
    with pytest.raises(ValueError):
        await repo.update_job(db, a, job_id="evil")
    assert await repo.get_job(db, "missing-job") is None
    assert any(j.job_id == a for j in await repo.list_jobs(db, status=JobStatus.COMPLETED, limit=500))


async def test_jobs_due_for_redaction(db: Database) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    due = await new_job(db, source="telegram", expires_at=past)
    later = await new_job(db, source="telegram", expires_at=datetime.now(UTC) + timedelta(hours=23))
    ids = await repo.jobs_due_for_redaction(db)
    assert due in ids and later not in ids


# ---- ephemeral inputs -----------------------------------------------------------------------
async def test_ephemeral_ttl_and_purge(db: Database) -> None:
    job = await new_job(db)
    short = await repo.put_ephemeral(
        db, kind="telegram_text", content="forwarded msg", ttl_s=3600, job_id=job
    )
    long = await repo.put_ephemeral(db, kind="chat_ref", content="chat-42", ttl_s=48 * 3600, job_id=job)
    assert short.content_sha256 == sha256_hex("forwarded msg")
    assert [e.id for e in await repo.list_ephemeral(db, job)] == [short.id, long.id]
    assert (await repo.get_ephemeral(db, short.id)).content == "forwarded msg"

    assert await repo.purge_expired_ephemeral(db) >= 0  # nothing of ours is expired yet
    assert await repo.get_ephemeral(db, short.id) is not None
    purged = await repo.purge_expired_ephemeral(db, now=datetime.now(UTC) + timedelta(hours=2))
    assert purged >= 1
    assert await repo.get_ephemeral(db, short.id) is None
    assert await repo.get_ephemeral(db, long.id) is not None
    remaining = await db.fetchval("SELECT count(*) FROM ephemeral_inputs WHERE id = $1", short.id)
    assert remaining == 0  # really deleted, not just hidden


# ---- evidence -------------------------------------------------------------------------------
async def test_evidence_insert_list_and_vector_search(db: Database) -> None:
    job = await new_job(db)
    emb = FakeEmbedder()
    texts = ["bridge opened in mumbai", "rain in chennai", "election results announced"]
    items = [evidence(i + 1, t) for i, t in enumerate(texts)]
    vecs = await emb.embed(texts, kind="passage")
    assert await repo.insert_evidence_items(db, job, items, vecs) == 3
    assert [i.id for i in await repo.list_evidence(db, job)] == ["E1", "E2", "E3"]

    q = (await emb.embed(["rain in chennai"]))[0]
    hits = await repo.search_evidence(db, q, job_id=job, limit=3)
    assert hits[0].item.id == "E2" and hits[0].similarity == pytest.approx(1.0, abs=1e-5)
    assert hits[0].item.published_at == datetime(2026, 10, 30, tzinfo=UTC)
    assert all(h.job_id == job for h in hits)
    assert await repo.search_evidence(db, q, job_id=job, min_similarity=0.99, limit=3) == hits[:1]
    assert all(h.job_id != job for h in await repo.search_evidence(db, q, exclude_job_id=job))

    # upsert keeps the stored embedding when none is given
    await repo.insert_evidence_items(db, job, [evidence(2, "rain in chennai (updated)")])
    hits = await repo.search_evidence(db, q, job_id=job, limit=1)
    assert hits[0].item.snippet == "rain in chennai (updated)"
    with pytest.raises(ValueError):
        await repo.insert_evidence_items(db, job, items[:1], np.zeros((1, 10)))


# ---- media + known fakes ----------------------------------------------------------------------
PHASH = "f0e1d2c3b4a59687"


def flip_bits(hex16: str, n: int) -> str:
    value = int(hex16, 16) ^ ((1 << n) - 1)
    return f"{value:016x}"


async def test_media_assets_phash_and_clip_search(db: Database) -> None:
    job_a, job_b = await new_job(db), await new_job(db)
    clip = unit(np.random.default_rng(1).standard_normal(512))
    asset = MediaAsset(
        id="m1", kind=MediaKind.IMAGE, path="C:/x/m1.jpg", sha256="ab" * 32, phash=PHASH, width=10, height=20
    )
    await repo.insert_media_assets(db, job_a, [asset], clip={"m1": clip})
    assert (await repo.list_media_assets(db, job_a))[0] == asset

    hits = await repo.search_media_by_phash(db, flip_bits(PHASH, 3), max_distance=5, exclude_job_id=job_b)
    mine = [h for h in hits if h.job_id == job_a]
    assert mine and mine[0].hamming == 3 and mine[0].phash == PHASH
    assert not [
        h
        for h in await repo.search_media_by_phash(db, flip_bits(PHASH, 12), max_distance=5)
        if h.job_id == job_a
    ]
    clip_hits = await repo.search_media_by_clip(db, clip, limit=5, min_similarity=0.99)
    assert any(h.job_id == job_a and h.similarity == pytest.approx(1.0, abs=1e-5) for h in clip_hits)
    assert [h.job_id for h in await repo.find_media_by_sha256(db, "AB" * 32)].count(job_a) == 1
    with pytest.raises(ValueError):
        await repo.search_media_by_phash(db, "not-hex")


async def test_phash_extreme_values_round_trip(db: Database) -> None:
    job = await new_job(db)
    assets = [
        MediaAsset(id=f"x{i}", kind=MediaKind.VIDEO_FRAME, path="p", sha256=f"{i:064x}", phash=p, frame_ts=i)
        for i, p in enumerate(["0000000000000000", "ffffffffffffffff", "8000000000000001"])
    ]
    await repo.insert_media_assets(db, job, assets)
    assert [a.phash for a in await repo.list_media_assets(db, job)] == [a.phash for a in assets]


async def test_known_fakes_upsert_and_searches(db: Database) -> None:
    fid = new_id("kf")
    clip = unit(np.random.default_rng(7).standard_normal(512))
    await repo.upsert_known_fake(
        db,
        id=fid,
        label="manipulated",
        source_urls=["https://factcheck.example/1"],
        title="Doctored photo",
        claim="X said Y",
        sha256="CD" * 32,
        phash=PHASH,
        clip=clip,
    )
    # re-seed without hashes/vectors must not wipe them
    await repo.upsert_known_fake(
        db,
        id=fid,
        label="manipulated",
        source_urls=["https://factcheck.example/1", "https://factcheck.example/2"],
        title="Doctored photo (v2)",
    )
    fake = await repo.get_known_fake(db, fid)
    assert fake.title == "Doctored photo (v2)" and fake.phash == PHASH and fake.has_clip
    assert fake.sha256 == "cd" * 32 and len(fake.source_urls) == 2

    hits = await repo.search_known_fakes_by_phash(db, flip_bits(PHASH, 2), max_distance=8)
    hit = next(h for h in hits if h.fake.id == fid)
    assert hit.hamming == 2
    match = hit.to_match()
    assert match.known_fake_id == fid and match.label == "manipulated" and match.hamming == 2
    clip_hits = await repo.search_known_fakes_by_clip(db, clip, min_similarity=0.95)
    assert clip_hits[0].fake.id == fid and clip_hits[0].clip_similarity == pytest.approx(1.0, abs=1e-5)
    assert [f.id for f in await repo.find_known_fakes_by_sha256(db, "cd" * 32)] == [fid]
    assert await repo.count_known_fakes(db) >= 1
    with pytest.raises(Exception, match="check"):
        await repo.upsert_known_fake(db, id=new_id("kf"), label="totally_fake")


# ---- forensic, reports, legal -----------------------------------------------------------------
async def test_forensic_report_and_legal_metadata(db: Database) -> None:
    job = await new_job(db)
    scores = [
        ModelScore(
            model_id="org/detector",
            provider="local",
            raw_score=0.91,
            weight=0.5,
            frames_scored=16,
            latency_ms=1200,
        )
    ]
    await repo.insert_forensic_scores(db, job, scores, asset_id="m1")
    assert await repo.list_forensic_scores(db, job) == scores

    attempt = await repo.insert_report_attempt(
        db,
        attempt_id=new_id("ra"),
        job_id=job,
        platform="x",
        method="playwright_guided",
        target_url="https://x.com/p/1",
    )
    assert await repo.set_report_attempt_audit(db, attempt.attempt_id, 42)
    assert (await repo.list_report_attempts(db, job))[0].audit_id == 42

    acks = LegalAcknowledgements(
        is_subject_or_authorized=True,
        not_legal_advice=True,
        no_auto_send=True,
        will_review_before_filing=True,
    )
    draft = await repo.insert_legal_draft(
        db,
        draft_id=new_id("ld"),
        job_id=job,
        jurisdiction="IN",
        template_version="in-v1",
        draft_text="Dear Grievance Officer ...",
        acknowledgements=acks,
    )
    assert draft.draft_sha256 == sha256_hex("Dear Grievance Officer ...")
    assert draft.acknowledgements["no_auto_send"] is True
    stored = await db.fetchrow("SELECT * FROM legal_drafts WHERE draft_id = $1", draft.draft_id)
    assert "Dear Grievance Officer" not in str(dict(stored))


# ---- tool cache + model registry -----------------------------------------------------------------
async def test_tool_cache_db_level(db: Database) -> None:
    key = new_job_id()
    await repo.cache_put(db, "gdelt", key, value={"articles": [1, 2]}, ttl_s=60, op="doc")
    value, remaining = await repo.cache_get(db, "gdelt", key)
    assert value == {"articles": [1, 2]} and 50 < remaining <= 60
    await repo.cache_put(db, "gdelt", key + "x", value=[1], ttl_s=0.001)
    await db.execute("SELECT pg_sleep(0.01)")
    assert await repo.cache_get(db, "gdelt", key + "x") is None
    assert await repo.purge_expired_cache(db) >= 1

    # ToolCache falls through memory -> DB, so a fresh process (empty memory) still hits
    cache = ToolCache(db)
    assert await cache.get("gdelt", key) == (True, {"articles": [1, 2]})
    await cache.set("gdelt", key + "y", {"z": 1}, 60, op="doc")
    assert await ToolCache(db).get("gdelt", key + "y") == (True, {"z": 1})


async def test_model_registry_upsert(db: Database) -> None:
    await repo.upsert_model(
        db, "llm_primary", provider="aicredits", model_id="openai/gpt-6-luna", meta={"probe_latency_ms": 900}
    )
    await repo.upsert_model(
        db, "llm_primary", provider="aicredits", model_id="openai/gpt-6-luna", meta={"probe_latency_ms": 800}
    )
    rows = {r.key: r for r in await repo.get_model_rows(db)}
    assert rows["llm_primary"].meta == {"probe_latency_ms": 800}
    assert (await repo.get_resolved_models(db))["llm_primary"] == "openai/gpt-6-luna"
