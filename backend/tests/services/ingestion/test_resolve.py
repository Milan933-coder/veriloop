"""resolve_input end to end with mocked HTTP, a stubbed yt-dlp and real ffmpeg (no network)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from veriloop.contracts import InputKind, MediaKind, Platform, VerifyRequest
from veriloop.core.errors import ToolAttemptError
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.ingestion import labels, resolve_input, ytdlp
from veriloop.services.ingestion.imaging import describe_image

from .helpers import make_image, make_video, needs_ffmpeg, new_job


@pytest.fixture
def no_browser(deps: Deps) -> None:
    """Headless Chromium is the last resort; in unit tests it fails fast instead of launching."""
    deps.tools.chaos.set("playwright_fetch", "error")


async def test_text_only_input(deps: Deps) -> None:
    job = await new_job(deps.db)
    out = await resolve_input(VerifyRequest(text="  The cabinet approved a 3% DA hike.  "), None, job, deps)
    assert out.input_kind is InputKind.TEXT and out.platform is Platform.TEXT
    assert out.caption_text == "The cabinet approved a 3% DA hike."
    assert out.media == [] and out.labels == [] and not out.media_accessible


async def test_upload_image_is_hashed_and_persisted(deps: Deps, tmp_path: Path) -> None:
    job = await new_job(deps.db)
    src = make_image(tmp_path / "in" / "photo.jpg", seed=3)
    out = await resolve_input(VerifyRequest(upload_id="u1", text="caption"), str(src), job, deps)
    assert out.input_kind is InputKind.UPLOAD and out.platform is Platform.UPLOAD
    assert out.media_accessible and len(out.media) == 1
    asset = out.media[0]
    info = describe_image(src)
    assert asset.kind is MediaKind.IMAGE and asset.source == "upload"
    assert asset.sha256 == info.sha256 and asset.phash == info.phash and len(asset.phash) == 16
    assert Path(asset.path).parent == deps.settings.media_dir(job)  # copied into the job's media dir
    stored = await repo.list_media_assets(deps.db, job)
    assert [(a.id, a.sha256, a.phash) for a in stored] == [(asset.id, asset.sha256, asset.phash)]


async def test_upload_too_large_is_text_only(make_settings, deps: Deps, tmp_path: Path) -> None:
    deps.settings = make_settings(max_upload_mb=0)
    job = await new_job(deps.db)
    src = make_image(tmp_path / "big.png", size=(64, 64))
    out = await resolve_input(VerifyRequest(upload_id="u", text="t"), str(src), job, deps)
    assert out.media == []
    assert labels.UPLOAD_TOO_LARGE.format(max_mb=0) in out.labels and labels.VISUAL_UNAVAILABLE in out.labels


@needs_ffmpeg
async def test_long_upload_video_is_capped_and_labelled(make_settings, deps: Deps, tmp_path: Path) -> None:
    """E8: frame cap spread across the video, mono 16 kHz audio, and a user-visible sampling note."""
    deps.settings = make_settings(frame_cap=4, frame_fps=1.0, audio_cap_s=5)
    job = await new_job(deps.db)
    src = make_video(tmp_path / "clip.mp4", 12)
    out = await resolve_input(VerifyRequest(upload_id="u", text="caption"), str(src), job, deps)
    assert out.video_path and out.audio_path and out.media_accessible
    assert [a.kind for a in out.media] == [MediaKind.VIDEO_FRAME] * 4
    assert [a.frame_ts for a in out.media] == [1.5, 4.5, 7.5, 10.5]
    assert out.sampling is not None and out.sampling.capped and out.sampling.frames == 4
    assert out.sampling.note == "Sampled 4 frames of 12 s video" and out.sampling.note in out.labels
    assert out.duration_s == pytest.approx(12, abs=0.5)
    from veriloop.services.extraction.asr import audio_seconds

    assert audio_seconds(out.audio_path) == pytest.approx(5, abs=0.2)  # AUDIO_CAP_S
    assert len(await repo.list_media_assets(deps.db, job)) == 4


@needs_ffmpeg
async def test_youtube_url_via_ytdlp(deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_video(tmp_path / "src.mp4", 5)

    async def fake_download(url: str, out_dir: Path, **_: Any) -> dict[str, Any]:
        dest = Path(out_dir) / "source.mp4"
        shutil.copy2(source, dest)
        info = {"title": "Super Typhoon Bavi", "uploader": "NDT", "upload_date": "20260706", "duration": 5}
        return {"info": info, "path": str(dest), "bytes": dest.stat().st_size, "partial": False}

    monkeypatch.setattr(ytdlp, "download", fake_download)
    job = await new_job(deps.db)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://www.youtube.com/oembed").respond(
            json={
                "title": "Super Typhoon Bavi (oEmbed)",
                "author_name": "NDT",
                "thumbnail_url": "https://i.ytimg.com/t.jpg",
            }
        )
        out = await resolve_input(
            VerifyRequest(url="https://youtube.com/shorts/XeYAGU1Z6l8?si=track"), None, job, deps
        )
    assert out.platform is Platform.YOUTUBE
    assert out.normalized_url == "https://www.youtube.com/watch?v=XeYAGU1Z6l8"
    assert "oembed" in out.fetch_methods and "yt-dlp" in out.fetch_methods and "ffmpeg" in out.fetch_methods
    assert out.title == "Super Typhoon Bavi (oEmbed)" and out.author == "NDT"
    assert out.published_at is not None and out.published_at.date().isoformat() == "2026-07-06"
    assert len(out.media) == 5 and out.media_accessible and out.labels == []
    assert out.sampling is not None and not out.sampling.capped


async def test_unreachable_link_falls_back_to_text_only(deps: Deps, no_browser: None) -> None:
    """E1: page fetch fails, browser fallback fails -> caption-only analysis with a visible label."""
    job = await new_job(deps.db)
    with respx.mock() as mock:
        mock.get("https://unknown.example/post/1").mock(return_value=httpx.Response(404))
        out = await resolve_input(
            VerifyRequest(url="https://unknown.example/post/1", text="Video shows a missile strike"),
            None,
            job,
            deps,
        )
    assert out.input_kind is InputKind.URL and out.platform is Platform.WEB
    assert out.caption_text == "Video shows a missile strike"
    assert labels.VISUAL_UNAVAILABLE in out.labels and out.media == [] and not out.media_accessible


async def test_login_walled_instagram_uses_caption(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, no_browser: None
) -> None:
    """E2: yt-dlp needs a login; og: metadata still gives the caption; label says caption-only."""

    async def login_wall(*_: Any, **__: Any) -> dict[str, Any]:
        raise ToolAttemptError("login_required", "Instagram: login required")

    monkeypatch.setattr(ytdlp, "download", login_wall)
    job = await new_job(deps.db)
    url = "https://www.instagram.com/reel/abc123/"
    html = (
        '<meta property="og:title" content="Reel by someone">'
        '<meta property="og:description" content="Missile hits the city tonight #breaking">'
        '<meta property="og:image" content="https://cdn.example/thumb.jpg">'
    )
    with respx.mock() as mock:
        mock.get(url).respond(200, text=html, headers={"content-type": "text/html"})
        mock.get("https://cdn.example/thumb.jpg").respond(404)
        out = await resolve_input(VerifyRequest(url=url + "?igsh=xyz"), None, job, deps)
    assert out.platform is Platform.INSTAGRAM and out.normalized_url == url
    assert labels.LOGIN_WALLED in out.labels
    assert out.caption_text == "Missile hits the city tonight #breaking"
    assert not out.media_accessible and out.video_path is None


async def test_short_link_is_expanded(deps: Deps, monkeypatch: pytest.MonkeyPatch, no_browser: None) -> None:
    async def no_video(*_: Any, **__: Any) -> dict[str, Any]:
        raise ToolAttemptError("no_media", "No video could be found in this tweet")

    monkeypatch.setattr(ytdlp, "download", no_video)
    job = await new_job(deps.db)
    target = "https://x.com/PIBFactCheck/status/2112600705232249021"
    with respx.mock(assert_all_called=False) as mock:
        mock.head("https://t.co/abc").respond(301, headers={"location": target + "?s=20"})
        mock.head(target + "?s=20").respond(200)
        mock.get("https://publish.twitter.com/oembed").respond(
            json={
                "author_name": "PIB Fact Check",
                "html": '<blockquote><p lang="en">DEEPFAKE VIDEO ALERT!</p>&mdash; '
                '<a href="https://x.com">October 20, 2026</a></blockquote>',
            }
        )
        mock.get("https://cdn.syndication.twimg.com/tweet-result").respond(404)
        out = await resolve_input(VerifyRequest(url="https://t.co/abc"), None, job, deps)
    assert out.platform is Platform.X and out.normalized_url == target
    assert "expand" in out.fetch_methods and "oembed" in out.fetch_methods
    assert out.caption_text == "DEEPFAKE VIDEO ALERT!" and out.author == "PIB Fact Check"
    assert labels.VISUAL_UNAVAILABLE in out.labels


async def test_forwarded_text_with_link_follows_the_link(deps: Deps, no_browser: None) -> None:
    job = await new_job(deps.db)
    with respx.mock() as mock:
        mock.get("https://unknown.example/a").respond(500)
        out = await resolve_input(
            VerifyRequest(text="Shocking!! https://unknown.example/a watch before deleted"), None, job, deps
        )
    assert out.input_kind is InputKind.URL and out.original_url == "https://unknown.example/a"
    assert out.caption_text == "Shocking!! watch before deleted"


async def test_never_raises_on_unexpected_errors(deps: Deps, monkeypatch: pytest.MonkeyPatch) -> None:
    """E6: any bug inside ingestion degrades to a text-only ResolvedInput instead of crashing the job."""
    from veriloop.services.ingestion import service

    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(service, "_from_url", boom)
    job = await new_job(deps.db)
    out = await resolve_input(VerifyRequest(url="https://example.org/x", text="claim"), None, job, deps)
    assert out.caption_text == "claim" and labels.VISUAL_UNAVAILABLE in out.labels
