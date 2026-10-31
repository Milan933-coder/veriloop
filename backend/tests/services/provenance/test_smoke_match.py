"""SMOKE (ml): the demo case (b) clip matches its seeded known fake by pHash AND CLIP; the stamped
fact-check thumbnail matches through its centre-crop variant.

    cd backend && $PY scripts/seed_known_fakes.py && $PY scripts/fetch_demo_media.py   # once (D:)
    cd backend && $PY -m pytest tests/services/provenance/test_smoke_match.py -m ml -s

Seeds KF-004 into the per-session test database with the same code the scripts use (the app's own
database is left untouched), then resolves the downloaded clip as an upload and runs provenance.
"""

from __future__ import annotations

import time

import pytest
from structlog.testing import capture_logs

from tests.services.ingestion.helpers import new_job
from tests.services.ingestion.smoke import emit, live_deps, live_settings
from veriloop.contracts import Claim, ClaimExtraction, MediaKind, VerifyRequest
from veriloop.core.executor import run_cpu
from veriloop.db.engine import Database
from veriloop.services import ingestion, provenance
from veriloop.services.ingestion.imaging import describe_image
from veriloop.services.provenance import seeding

pytestmark = [pytest.mark.ml, pytest.mark.timeout(900)]  # cold model loads exceed the 120 s default

CLAIM = "This video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh."


async def test_case_b_matches_known_fake(db: Database) -> None:
    settings = live_settings()
    video = settings.home / "demo" / "case_b" / "demo_case_b.mp4"
    thumb = settings.home / "seed" / "KF-004.webp"
    if not video.exists() or not thumb.exists():
        pytest.skip("run scripts/seed_known_fakes.py and scripts/fetch_demo_media.py first")
    entry = next(e for e in seeding.read_manifest() if e.id == "KF-004")
    async with live_deps(db, llm=False) as deps:
        t = time.perf_counter()
        await provenance.prewarm(deps)
        await deps.embedder.load()  # the app prewarms e5 at startup too; claim similarity uses it
        report: dict = {"prewarm_s": round(time.perf_counter() - t, 1)}
        # Seed exactly like the scripts: thumbnail (+ crop) and the clip's sampled frames with origin.
        images = await run_cpu(seeding.image_variants_blocking, thumb, entry.id, settings)
        await seeding.upsert_entry(deps.db, entry, images)
        frames_dir = settings.home / "tmp" / "smoke_case_b_frames"
        frames = await run_cpu(seeding.sample_video_frames_blocking, video, frames_dir, settings)
        frame_rows = []
        for i, frame in enumerate(frames, start=1):
            frame_rows += await run_cpu(
                seeding.image_variants_blocking, frame, f"{entry.id}~demo-b-f{i:02d}", settings, crop=False
            )
        await seeding.upsert_entry(
            deps.db, entry, frame_rows, origin=("https://www.youtube.com/watch?v=XeYAGU1Z6l8", "2026-07-06")
        )
        report["seeded_rows"] = len(images) + len(frame_rows)

        job = await new_job(deps.db)
        resolved = await ingestion.resolve_input(
            VerifyRequest(upload_id="smoke", text=CLAIM), str(video), job, deps
        )
        extraction = ClaimExtraction(
            claims=[Claim(id="C1", text=CLAIM, text_en=CLAIM, location="Visakhapatnam")],
            primary_claim_id="C1",
        )
        t = time.perf_counter()
        with capture_logs() as logs:
            result = await provenance.run_provenance(job, resolved, extraction, deps)
        report["video"] = {
            "seconds": round(time.perf_counter() - t, 2),
            "steps_ms": next(e for e in logs if e.get("event") == "provenance_timings")["timings_ms"],
            "matches": [
                m.model_dump(include={"known_fake_id", "label", "hamming", "clip_similarity"})
                for m in result.known_fake_matches
            ],
            "items": [(i.id, i.url, i.retrieval_method.value, i.stance.value) for i in result.items],
            "first_seen": result.first_seen_estimate.model_dump(
                include={"url", "first_seen_at", "timestamp_source"}
            )
            if result.first_seen_estimate
            else None,
            "out_of_context_score": result.out_of_context_score,
            "tools_used": result.tools_used,
        }
        # The stamped fact-check thumbnail itself (a still image input).
        info = describe_image(thumb)
        still = resolved.model_copy(
            update={
                "media": [
                    resolved.media[0].model_copy(
                        update={
                            "id": "M1",
                            "kind": MediaKind.IMAGE,
                            "path": str(thumb),
                            "sha256": info.sha256,
                            "phash": info.phash,
                        }
                    )
                ],
                "video_path": None,
            }
        )
        thumb_result = await provenance.run_provenance(job, still, extraction, deps)
        report["thumbnail"] = [
            m.model_dump(include={"known_fake_id", "hamming", "clip_similarity"})
            for m in thumb_result.known_fake_matches
        ]
    emit("provenance_case_b", report)

    match = next(m for m in result.known_fake_matches if m.known_fake_id == "KF-004")
    assert match.hamming is not None and match.hamming <= 10
    assert match.clip_similarity is not None and match.clip_similarity >= 0.93
    assert set(match.source_urls) == set(entry.factcheck_urls)
    assert result.out_of_context_score > 0  # earliest copy (2026-07-06) long before the claimed event
    assert any(m.known_fake_id == "KF-004" for m in thumb_result.known_fake_matches)
