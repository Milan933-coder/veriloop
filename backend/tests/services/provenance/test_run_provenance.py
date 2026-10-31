"""run_provenance against the real test database (pHash/CLIP index), keyed tools mocked with respx."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import respx
from PIL import Image

from tests.services.ingestion.helpers import make_image, new_job
from veriloop.contracts import (
    BranchStatus,
    Claim,
    ClaimExtraction,
    InputKind,
    MediaAsset,
    MediaKind,
    Platform,
    ResolvedInput,
    RetrievalMethod,
    Stance,
)
from veriloop.core.tool_client import ToolClient
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.ingestion.imaging import describe_image, phash_hex
from veriloop.services.provenance import clip, run_provenance, service, web
from veriloop.services.provenance.index import format_origin

CLAIM = "Video shows Cyclone Arnab hitting Visakhapatnam on 28 October 2026"
FACT_CHECKS = [
    "https://newschecker.in/fact-check/fact-check/old-video-shared-as-cyclone-arnab-wreaking-havoc-in-visakhapatnam",
    "https://www.thequint.com/news/webqoof/old-video-from-us-territory-falsely-linked-to-cyclone-arnab-visakhapatnam-fact-check",
]


def extraction(text: str = CLAIM) -> ClaimExtraction:
    return ClaimExtraction(claims=[Claim(id="C1", text=text, text_en=text)], primary_claim_id="C1")


def asset_for(path: Path, asset_id: str = "M1", kind: MediaKind = MediaKind.VIDEO_FRAME) -> MediaAsset:
    info = describe_image(path)
    return MediaAsset(
        id=asset_id,
        kind=kind,
        path=str(path),
        sha256=info.sha256,
        phash=info.phash,
        width=info.width,
        height=info.height,
    )


def resolved_with(*assets: MediaAsset, url: str | None = None) -> ResolvedInput:
    return ResolvedInput(
        input_kind=InputKind.URL if url else InputKind.UPLOAD,
        platform=Platform.WEB if url else Platform.UPLOAD,
        normalized_url=url,
        media=list(assets),
        media_accessible=True,
    )


def reencoded(src: Path, dest: Path, quality: int = 70) -> Path:
    Image.open(src).convert("RGB").save(dest, format="JPEG", quality=quality)
    return dest


async def seed_fake(deps: Deps, fake_id: str, image: Path, **kw: Any) -> str:
    img = Image.open(image).convert("RGB")
    await repo.upsert_known_fake(
        deps.db,
        id=fake_id,
        label=kw.pop("label", "out_of_context"),
        source_urls=kw.pop("source_urls", FACT_CHECKS),
        title=kw.pop("title", "Video shows Cyclone Arnab hitting Visakhapatnam"),
        claim=kw.pop(
            "claim", "Video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh, in October 2026."
        ),
        phash=phash_hex(img),
        first_seen_at=kw.pop("first_seen_at", datetime(2026, 10, 28, tzinfo=UTC)),
        **kw,
    )
    return fake_id


async def test_known_fake_phash_match(deps: Deps, tmp_path: Path) -> None:
    original = make_image(tmp_path / "orig.jpg", size=(480, 360), seed=101)
    await seed_fake(deps, "KF-T101", original)
    job = await new_job(deps.db)
    frame = reencoded(original, tmp_path / "frame.jpg")  # a re-upload: different bytes, same picture
    out = await run_provenance(job, resolved_with(asset_for(frame)), extraction(), deps)

    assert out.branch_status is BranchStatus.OK
    match = out.known_fake_matches[0]
    assert match.known_fake_id == "KF-T101" and match.label == "out_of_context"
    assert match.hamming is not None and match.hamming <= 10 and match.source_urls == FACT_CHECKS
    # Citable items carry exactly the manifest's fact-check URLs (never synthesized).
    assert [i.url for i in out.items] == FACT_CHECKS and [i.id for i in out.items] == ["P1", "P2"]
    item = out.items[0]
    assert item.retrieval_method is RetrievalMethod.PHASH_INDEX and item.branch == "provenance"
    assert item.stance is Stance.REFUTE and item.stance_quote and item.stance_quote in item.snippet
    assert item.claimreview is not None and item.claimreview.normalized_rating == -1.0
    assert not item.claimreview.manipulation_rating  # out_of_context is not a manipulation
    assert item.publisher_group != out.items[1].publisher_group
    assert (
        out.first_seen_estimate is not None
        and out.first_seen_estimate.timestamp_source == "known_fake_manifest"
    )
    assert any("Cloud Vision web detection unconfigured" in n for n in out.notes)
    assert any("CLIP skipped (ML_ENABLE=false)" in n for n in out.notes)


async def test_variant_rows_merge_and_origin_drives_first_seen(deps: Deps, tmp_path: Path) -> None:
    """A demo-frame variant (KF-T102~demo-f01) matches; its [origin] date predates the claimed event."""
    frame_src = make_image(tmp_path / "f.jpg", size=(480, 360), seed=102)
    await seed_fake(deps, "KF-T102", make_image(tmp_path / "thumb.jpg", seed=103))
    origin = format_origin("https://www.youtube.com/watch?v=XeYAGU1Z6l8", "2026-07-06")
    await seed_fake(deps, "KF-T102~demo-f01", frame_src, notes=f"Typhoon Bavi footage {origin}")
    job = await new_job(deps.db)
    out = await run_provenance(job, resolved_with(asset_for(frame_src)), extraction(), deps)

    assert [m.known_fake_id for m in out.known_fake_matches] == ["KF-T102"]
    assert out.known_fake_matches[0].notes == "Typhoon Bavi footage"  # origin tag stripped for display
    first = out.first_seen_estimate
    assert first is not None and first.url == "https://www.youtube.com/watch?v=XeYAGU1Z6l8"
    assert first.first_seen_at == datetime(2026, 7, 6, tzinfo=UTC)
    assert out.out_of_context_score == 1.0  # 114 days before the claimed 28 Oct 2026 event
    assert any("predates the date stated in the claim" in n for n in out.notes)


async def test_no_match_far_image(deps: Deps, tmp_path: Path) -> None:
    job = await new_job(deps.db)
    lone = make_image(tmp_path / "lone.jpg", seed=104)
    out = await run_provenance(job, resolved_with(asset_for(lone)), extraction(), deps)
    assert out.known_fake_matches == [] and out.items == [] and out.out_of_context_score == 0.0
    assert any("No match in the known-fake index" in n for n in out.notes)


async def test_prior_run_match(deps: Deps, tmp_path: Path) -> None:
    img = make_image(tmp_path / "p.jpg", seed=105)
    first_job = await new_job(deps.db, url="https://www.facebook.com/reel/1815114716474947")
    await repo.insert_media_assets(deps.db, first_job, [asset_for(img)])
    job = await new_job(deps.db)
    out = await run_provenance(job, resolved_with(asset_for(img)), extraction("A photo of a flood"), deps)
    assert out.prior_run_matches == [first_job]
    app = next(a for a in out.appearances if a.timestamp_source == "veriloop_prior_run")
    assert app.url == "https://www.facebook.com/reel/1815114716474947" and app.first_seen_at is not None


async def test_context_flag_from_lists(deps: Deps, tmp_path: Path) -> None:
    img = make_image(tmp_path / "s.jpg", seed=106)
    await seed_fake(deps, "KF-T106", img, source_urls=["https://www.theonion.com/some-satire-story"])
    job = await new_job(deps.db)
    out = await run_provenance(job, resolved_with(asset_for(img)), extraction(), deps)
    assert out.context_flags == ["satire"]
    assert any("listed as satire (The Onion; satire.json" in n for n in out.notes)


async def test_clip_near_duplicate_and_vectors_stored(
    deps: Deps, make_settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps.settings = make_settings(ml_enable=True)
    vec = np.zeros(512, dtype=np.float32)
    vec[7] = 1.0
    monkeypatch.setattr(clip, "embed_images_blocking", lambda images, settings: np.stack([vec] * len(images)))
    other = make_image(tmp_path / "other.jpg", seed=107)  # different pixels: only CLIP can match
    await repo.upsert_known_fake(
        deps.db,
        id="KF-T107",
        label="manipulated",
        source_urls=[FACT_CHECKS[0]],
        claim="Senator deepfake",
        clip=vec,
    )
    job = await new_job(deps.db)
    frame = make_image(tmp_path / "frame.jpg", seed=108)
    out = await run_provenance(job, resolved_with(asset_for(frame)), extraction(), deps)
    match = next(m for m in out.known_fake_matches if m.known_fake_id == "KF-T107")
    assert match.hamming is None and match.clip_similarity == pytest.approx(1.0)
    assert out.items[0].retrieval_method is RetrievalMethod.CLIP_INDEX
    assert out.items[0].claimreview is not None and out.items[0].claimreview.manipulation_rating
    stored = await repo.search_media_by_clip(deps.db, vec, min_similarity=0.99)
    assert any(h.job_id == job for h in stored)  # this job's vectors are indexed for future runs
    del other


async def test_vision_web_when_configured(make_settings: Any, deps: Deps, tmp_path: Path) -> None:
    settings = make_settings(google_vision_api_key="vision-key")
    deps.settings = settings
    deps.tools = ToolClient(settings, cache=deps.tools.cache)
    img = make_image(tmp_path / "v.jpg", seed=109)
    job = await new_job(deps.db)
    page = "https://www.theonion.com/typhoon-photo"
    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(web.VISION_URL).respond(
                json={
                    "responses": [
                        {
                            "webDetection": {
                                "pagesWithMatchingImages": [
                                    {"url": page, "pageTitle": "Typhoon", "fullMatchingImages": [{}]}
                                ]
                            }
                        }
                    ]
                }
            )
            mock.get(page).respond(
                200,
                text='<meta property="article:published_time" content="2026-07-07T10:00:00Z">',
                headers={"content-type": "text/html"},
            )
            out = await run_provenance(job, resolved_with(asset_for(img)), extraction(), deps)
        assert route.called and route.calls[0].request.url.params["key"] == "vision-key"
        app = next(a for a in out.appearances if a.url == page)
        assert app.match_type == "full" and app.first_seen_at == datetime(2026, 7, 7, 10, tzinfo=UTC)
        assert app.timestamp_source == "article:published_time" and app.context_flags == ["satire"]
        item = next(i for i in out.items if i.url == page)
        assert item.retrieval_method is RetrievalMethod.VISION_WEB_DETECTION and item.published_at is not None
        assert out.out_of_context_score == 1.0 and "vision_web" in out.tools_used
    finally:
        await deps.tools.aclose()


async def test_text_only_is_skipped(deps: Deps) -> None:
    out = await run_provenance(
        "job", ResolvedInput(input_kind=InputKind.TEXT, platform=Platform.TEXT), extraction(), deps
    )
    assert out.branch_status is BranchStatus.SKIPPED and out.known_fake_matches == []


async def test_never_raises(deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(service, "_run", boom)
    img = make_image(tmp_path / "x.jpg", seed=110)
    out = await run_provenance("job", resolved_with(asset_for(img)), extraction(), deps)
    assert out.branch_status is BranchStatus.FAILED and out.errors[0].error_type == "exception"


async def test_upsert_entry_variants(deps: Deps, settings: Any, tmp_path: Path) -> None:
    from veriloop.services.provenance import seeding

    entry = next(e for e in seeding.read_manifest() if e.id == "KF-004")
    entry = seeding.ManifestEntry(**{**entry.__dict__, "id": "KF-T111"})
    path = make_image(tmp_path / "kf.jpg", seed=111)
    images = seeding.image_variants_blocking(path, entry.id, settings, use_clip=False)
    assert await seeding.upsert_entry(deps.db, entry, images) == 2
    rows = {
        r.id: r
        for r in await repo.list_known_fakes(deps.db, label="out_of_context")
        if r.id.startswith("KF-T111")
    }
    assert set(rows) == {"KF-T111", "KF-T111~crop70"}
    assert rows["KF-T111"].source_urls == list(entry.factcheck_urls) and rows["KF-T111"].sha256
    # Re-seeding without media keeps the stored hashes (idempotent).
    assert await seeding.upsert_entry(deps.db, entry, []) == 1
    again = await repo.get_known_fake(deps.db, "KF-T111")
    assert again is not None and again.phash == rows["KF-T111"].phash
