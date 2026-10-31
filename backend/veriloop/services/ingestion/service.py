"""resolve_input: turn a VerifyRequest (URL / upload / text) into a ResolvedInput (BUILD_SPEC §5).

URL path, cheapest first: normalise -> expand short links -> oEmbed ∥ yt-dlp (∥ page meta) ->
X syndication JSON for X image/text posts -> og:image / thumbnail -> headless Chromium as the last
resort (E1). Video goes through ffprobe -> sampled frames (FRAME_FPS, FRAME_CAP spread evenly, E8)
∥ 16 kHz mono audio (AUDIO_CAP_S). Every image/frame gets sha256 + 64-bit pHash and is persisted in
`media_assets`. Nothing here raises to the orchestrator: failures become labels + a text-only result.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from veriloop.contracts import (
    InputKind,
    MediaAsset,
    MediaKind,
    Platform,
    ResolvedInput,
    SamplingInfo,
    ToolError,
    VerifyRequest,
)
from veriloop.core.executor import run_cpu
from veriloop.db import repo
from veriloop.deps import Deps

from . import ffmpeg, labels, ytdlp
from .fetchers import (
    download_image,
    expand_short_link,
    fetch_oembed,
    fetch_page,
    fetch_x_syndication,
    localize_file,
    playwright_capture,
)
from .imaging import describe_image, is_image_file
from .pagemeta import clean_caption, parse_date, parse_page_meta, parse_x_oembed_html
from .urls import detect_platform, extract_urls, is_short_link, normalize_url, text_without_urls

log = structlog.get_logger(__name__)

# Platforms whose posts are usually a video yt-dlp can fetch keyless.
_VIDEO_PLATFORMS = {
    Platform.YOUTUBE,
    Platform.X,
    Platform.TIKTOK,
    Platform.INSTAGRAM,
    Platform.FACEBOOK,
    Platform.TELEGRAM,
}
_LOGIN_PLATFORMS = {Platform.INSTAGRAM, Platform.FACEBOOK}
_X_STATUS = re.compile(r"/status(?:es)?/(\d{5,25})")


@dataclass
class _Acc:
    """Everything learned while resolving one input (mutable scratch state)."""

    job_id: str
    media_dir: Path
    fetch_methods: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    errors: list[ToolError] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    title: str | None = None
    author: str | None = None
    post_text: str | None = None
    published_at: datetime | None = None
    oembed: dict[str, Any] | None = None
    image_urls: list[str] = field(default_factory=list)  # the post's own photos
    thumbnail_urls: list[str] = field(default_factory=list)  # posters / og:image previews
    login_walled: bool = False
    og_video: bool = False  # the page declares an embedded video (og:video / og:type video)
    media: list[MediaAsset] = field(default_factory=list)
    video_path: str | None = None
    audio_path: str | None = None
    duration_s: float | None = None
    sampling: SamplingInfo | None = None

    def method(self, name: str) -> None:
        if name not in self.fetch_methods:
            self.fetch_methods.append(name)

    def label(self, text: str) -> None:
        if text not in self.labels:
            self.labels.append(text)

    def fill(self, *, title: Any = None, author: Any = None, text: Any = None, published: Any = None) -> None:
        """First non-empty value wins: sources are consulted in decreasing reliability."""
        if title and not self.title:
            self.title = str(title).strip()[:500]
        if author and not self.author:
            self.author = str(author).strip()[:200]
        if text and not self.post_text:
            self.post_text = clean_caption(str(text))
        if published and not self.published_at:
            self.published_at = parse_date(published)


class _Timer:
    def __init__(self, acc: _Acc, step: str) -> None:
        self.acc, self.step = acc, step

    def __enter__(self) -> None:
        self.t = time.perf_counter()

    def __exit__(self, *exc: object) -> None:
        self.acc.timings_ms[self.step] = int((time.perf_counter() - self.t) * 1000)


# =============================================================================================
# public entry point
# =============================================================================================
async def resolve_input(
    request: VerifyRequest, upload_path: str | None, job_id: str, deps: Deps
) -> ResolvedInput:
    started = time.perf_counter()
    try:
        resolved, acc = await _resolve(request, upload_path, job_id, deps)
    except Exception as exc:  # E6: never crash the request; degrade to a text-only analysis
        log.exception("resolve_input_failed", job_id=job_id, error=f"{type(exc).__name__}: {exc}")
        resolved = _text_only(request, labels=[labels.VISUAL_UNAVAILABLE] if request.url else [])
        acc = None
    log.info(
        "resolve_input_done",
        job_id=job_id,
        input_kind=resolved.input_kind.value,
        platform=resolved.platform.value,
        media=len(resolved.media),
        media_accessible=resolved.media_accessible,
        fetch_methods=resolved.fetch_methods,
        labels=resolved.labels,
        tool_errors=[f"{e.tool}:{e.op}:{e.error_type}" for e in acc.errors] if acc else [],
        timings_ms=acc.timings_ms if acc else {},
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return resolved


def _text_only(request: VerifyRequest, *, labels: list[str]) -> ResolvedInput:
    url = request.url
    platform = request.platform or (detect_platform(normalize_url(url)) if url else Platform.TEXT)
    return ResolvedInput(
        input_kind=InputKind.URL if url else InputKind.TEXT,
        platform=platform,
        original_url=url,
        normalized_url=normalize_url(url) if url else None,
        caption_text=(request.text or "").strip() or None,
        labels=labels,
    )


async def _resolve(
    request: VerifyRequest, upload_path: str | None, job_id: str, deps: Deps
) -> tuple[ResolvedInput, _Acc]:
    acc = _Acc(job_id=job_id, media_dir=deps.settings.media_dir(job_id))
    user_text = (request.text or "").strip() or None
    url = (request.url or "").strip() or None
    if not url and not upload_path and user_text:
        # A forwarded message that carries a link: follow the link, keep the words as the caption.
        found = extract_urls(user_text)
        if found:
            url = found[0]
            user_text = text_without_urls(user_text) or None

    if upload_path:
        kind, platform = InputKind.UPLOAD, request.platform or Platform.UPLOAD
        await _from_upload(Path(upload_path), deps, acc)
        original = normalized = None
    elif url:
        kind = InputKind.URL
        original = url
        normalized, platform = await _from_url(url, request.platform, deps, acc)
    else:
        kind, platform = InputKind.TEXT, request.platform or Platform.TEXT
        original = normalized = None

    accessible = any(a.source in ("download", "upload") for a in acc.media) or bool(acc.audio_path)
    if kind is not InputKind.TEXT and not any(a.source in ("download", "upload") for a in acc.media):
        acc.label(labels.LOGIN_WALLED if acc.login_walled else labels.VISUAL_UNAVAILABLE)
        if any(a.source == "thumbnail" for a in acc.media):
            acc.label(labels.THUMBNAIL_ONLY)  # a preview is kept for provenance, never for forensics
    if acc.sampling is not None and acc.sampling.capped and acc.sampling.note:
        acc.label(acc.sampling.note)  # E8: the sampling caveat is user-visible

    caption = _join_caption(user_text, acc.post_text)
    resolved = ResolvedInput(
        input_kind=kind,
        platform=platform,
        original_url=original,
        normalized_url=normalized,
        title=acc.title,
        author=acc.author,
        caption_text=caption,
        published_at=acc.published_at,
        oembed=acc.oembed,
        media=acc.media,
        video_path=acc.video_path,
        audio_path=acc.audio_path,
        duration_s=acc.duration_s,
        sampling=acc.sampling,
        media_accessible=accessible,
        fetch_methods=acc.fetch_methods,
        labels=acc.labels,
    )
    await _persist(deps, job_id, acc.media)
    return resolved, acc


def _join_caption(user_text: str | None, post_text: str | None) -> str | None:
    parts: list[str] = []
    for part in (user_text, post_text):
        if part and not any(part.strip() in p for p in parts):
            parts.append(part.strip())
    return "\n\n".join(parts) or None


async def _persist(deps: Deps, job_id: str, media: list[MediaAsset]) -> None:
    if not media:
        return
    try:
        await repo.insert_media_assets(deps.db, job_id, media)
    except Exception as exc:  # e.g. no jobs row (scripts/tests): hashes still travel in ResolvedInput
        log.warning("media_assets_persist_failed", job_id=job_id, error=f"{type(exc).__name__}: {exc}")


# =============================================================================================
# uploads
# =============================================================================================
def _stage_upload(src: Path, media_dir: Path, max_bytes: int) -> tuple[Path | None, str | None]:
    """Copy the upload into the job's media dir (if the API stored it elsewhere). Blocking I/O."""
    if not src.is_file():
        return None, "missing"
    if src.stat().st_size > max_bytes:
        return None, "too_large"
    if src.resolve().parent == media_dir.resolve():
        return src, None
    dest = media_dir / f"upload{src.suffix.lower()}"
    shutil.copy2(src, dest)
    return dest, None


async def _from_upload(src: Path, deps: Deps, acc: _Acc) -> None:
    acc.method("upload")
    settings = deps.settings
    dest, problem = await asyncio.to_thread(_stage_upload, src, acc.media_dir, settings.max_upload_mb * 2**20)
    if problem == "too_large":
        acc.label(labels.UPLOAD_TOO_LARGE.format(max_mb=settings.max_upload_mb))
    if dest is None:
        log.warning("upload_unusable", path=str(src), problem=problem)
        return
    if await run_cpu(is_image_file, dest):
        acc.media = await run_cpu(_describe_assets, [(dest, None)], MediaKind.IMAGE, "upload")
        return
    await _process_video(dest, deps, acc, source="upload")


# =============================================================================================
# URLs
# =============================================================================================
async def _from_url(url: str, requested: Platform | None, deps: Deps, acc: _Acc) -> tuple[str, Platform]:
    tools = deps.tools
    normalized = normalize_url(url)
    if is_short_link(normalized):
        with _Timer(acc, "expand"):
            res = await expand_short_link(tools, normalized)
        if res.ok and res.value.get("final_url"):
            normalized = normalize_url(res.value["final_url"])
            acc.method("expand")
        elif res.error:
            acc.errors.append(res.error)
    detected = detect_platform(normalized)
    platform = detected if detected is not Platform.WEB else (requested or Platform.WEB)

    async def oembed_step() -> None:
        with _Timer(acc, "oembed"):
            res = await fetch_oembed(tools, platform, normalized)
        if res is None:
            return
        if not res.ok:
            acc.errors.append(res.error)  # type: ignore[arg-type]
            return
        _apply_oembed(platform, res.value, acc)

    async def ytdlp_step() -> dict[str, Any] | None:
        if platform not in _VIDEO_PLATFORMS:
            return None
        return await _run_ytdlp(normalized, deps, acc)

    oembed_done, video = await asyncio.gather(oembed_step(), ytdlp_step())
    del oembed_done

    if platform is Platform.X and not acc.video_path:
        await _x_syndication(normalized, deps, acc)

    if not acc.post_text or platform in (Platform.WEB, *_LOGIN_PLATFORMS, Platform.TELEGRAM):
        if not (platform is Platform.X and acc.post_text):
            await _page_meta(normalized, platform, deps, acc)
            # A web page that embeds a video: try yt-dlp's generic extractor once (only then).
            if platform is Platform.WEB and video is None and acc.og_video:
                video = await _run_ytdlp(normalized, deps, acc)

    if not acc.video_path:
        await _fetch_images(deps, acc)

    if not (acc.post_text or acc.title) or (not acc.media and not acc.video_path and acc.login_walled):
        await _playwright(normalized, deps, acc)
    return normalized, platform


def _apply_oembed(platform: Platform, data: Any, acc: _Acc) -> None:
    if not isinstance(data, dict):
        return
    acc.method("oembed")
    acc.oembed = {k: v for k, v in data.items() if k != "html" and isinstance(v, str | int | float | bool)}
    text = None
    published = None
    if platform is Platform.X:
        parsed = parse_x_oembed_html(str(data.get("html") or ""))
        text, published = parsed.get("text"), parsed.get("date_text")
    acc.fill(title=data.get("title"), author=data.get("author_name"), text=text, published=published)
    if data.get("thumbnail_url"):
        acc.thumbnail_urls.append(str(data["thumbnail_url"]))


async def _run_ytdlp(url: str, deps: Deps, acc: _Acc) -> dict[str, Any] | None:
    settings = deps.settings
    max_bytes = settings.max_upload_mb * 2**20
    key = {"url": url, "max_mb": settings.max_upload_mb, "max_video_s": settings.max_video_s, "res": 720}
    with _Timer(acc, "ytdlp"):
        res = await deps.tools.call(
            "ytdlp",
            "download",
            key,
            lambda: ytdlp.download(
                url, acc.media_dir, max_bytes=max_bytes, max_video_s=settings.max_video_s, max_res=720
            ),
        )
    if not res.ok:
        err = res.error
        assert err is not None
        acc.errors.append(err)
        if err.error_type == "login_required":
            acc.login_walled = True
        return None
    value = res.value or {}
    info = value.get("info") or {}
    acc.fill(
        title=info.get("title"),
        author=info.get("uploader") or info.get("channel"),
        text=info.get("description"),
        published=info.get("timestamp") or info.get("upload_date"),
    )
    if info.get("thumbnail"):
        acc.thumbnail_urls.append(str(info["thumbnail"]))
    path = localize_file(value.get("path"), acc.media_dir)
    if path is None:
        log.warning("ytdlp_file_missing", recorded=value.get("path"), replayed=res.replayed)
        return value
    acc.method("yt-dlp")
    full_duration = info.get("duration") if isinstance(info.get("duration"), int | float) else None
    await _process_video(path, deps, acc, source="download", full_duration=full_duration)
    if value.get("partial"):
        acc.label(labels.PARTIAL_DOWNLOAD.format(max_s=settings.max_video_s))
    return value


async def _x_syndication(url: str, deps: Deps, acc: _Acc) -> None:
    match = _X_STATUS.search(url)
    if not match:
        return
    with _Timer(acc, "x_syndication"):
        res = await fetch_x_syndication(deps.tools, match.group(1))
    if not res.ok or not isinstance(res.value, dict):
        if res.error:
            acc.errors.append(res.error)
        return
    data = res.value
    acc.method("x_syndication")
    note = (data.get("note_tweet") or {}).get("text") if isinstance(data.get("note_tweet"), dict) else None
    user = data.get("user") or {}
    acc.fill(author=user.get("name"), text=note or data.get("text"), published=data.get("created_at"))
    for photo in data.get("photos") or []:
        if isinstance(photo, dict) and photo.get("url"):
            acc.image_urls.append(str(photo["url"]))
    poster = (data.get("video") or {}).get("poster") if isinstance(data.get("video"), dict) else None
    if poster:
        acc.thumbnail_urls.append(str(poster))


async def _page_meta(url: str, platform: Platform, deps: Deps, acc: _Acc) -> None:
    with _Timer(acc, "page_meta"):
        res = await fetch_page(deps.tools, url)
    if not res.ok:
        acc.errors.append(res.error)  # type: ignore[arg-type]
        if res.error and "HTTP 40" in res.error.message and platform in _LOGIN_PLATFORMS:
            acc.login_walled = True
        return
    value = res.value or {}
    meta = parse_page_meta(value.get("html") or "", value.get("final_url") or url)
    if meta["status"] == "login":
        acc.login_walled = True
    if meta["status"] != "ok":
        return
    acc.method("page_meta")
    acc.fill(
        title=meta["title"], author=meta["author"], text=meta["description"], published=meta["published"]
    )
    if meta["image"]:
        # On a generic web page the og:image is the shared picture itself; on platforms it is a preview.
        (acc.image_urls if platform is Platform.WEB else acc.thumbnail_urls).append(meta["image"])
    acc.og_video = bool(meta["video"]) or str(meta.get("og_type") or "").startswith("video")


async def _fetch_images(deps: Deps, acc: _Acc) -> None:
    """Download the post's photos (media) or, failing that, one preview thumbnail (not forensic-grade)."""
    targets: list[tuple[str, MediaKind, str]] = [(u, MediaKind.IMAGE, "download") for u in acc.image_urls[:4]]
    if not targets and acc.thumbnail_urls:
        targets = [(acc.thumbnail_urls[0], MediaKind.THUMBNAIL, "thumbnail")]
    if not targets:
        return
    with _Timer(acc, "images"):
        results = await asyncio.gather(
            *(
                download_image(deps.tools, u, acc.media_dir / f"image_{i:02d}.img")
                for i, (u, _, _) in enumerate(targets)
            )
        )
    for (_, kind, source), res in zip(targets, results, strict=True):
        if not res.ok:
            acc.errors.append(res.error)  # type: ignore[arg-type]
            continue
        path = localize_file((res.value or {}).get("path"), acc.media_dir)
        if path is None:
            continue
        try:
            assets = await run_cpu(_describe_assets, [(path, None)], kind, source, len(acc.media))
        except ValueError as exc:
            log.info("image_undecodable", error=str(exc))
            continue
        acc.media.extend(assets)
        acc.method("image" if kind is MediaKind.IMAGE else "thumbnail")


async def _playwright(url: str, deps: Deps, acc: _Acc) -> None:
    with _Timer(acc, "playwright"):
        res = await playwright_capture(deps.tools, url, acc.media_dir / "page_capture.jpg")
    if not res.ok:
        acc.errors.append(res.error)  # type: ignore[arg-type]
        return
    value = res.value or {}
    meta = value.get("meta") or {}
    acc.method("playwright")
    acc.label(labels.PAGE_CAPTURE)
    page_text = str(value.get("text") or "")
    if any(m in page_text.lower()[:600] for m in ("log in", "sign up", "login")) and not meta.get(
        "og:description"
    ):
        acc.login_walled = acc.login_walled or detect_platform(url) in _LOGIN_PLATFORMS
    acc.fill(
        title=meta.get("og:title") or value.get("title"),
        author=meta.get("author"),
        text=meta.get("og:description") or meta.get("description") or page_text[:1500],
        published=meta.get("article:published_time"),
    )
    shot = localize_file(value.get("screenshot"), acc.media_dir)
    if shot is not None:
        try:
            acc.media.extend(
                await run_cpu(
                    _describe_assets, [(shot, None)], MediaKind.THUMBNAIL, "page_capture", len(acc.media)
                )
            )
        except ValueError:
            pass


# =============================================================================================
# video -> frames + audio
# =============================================================================================
async def _process_video(
    path: Path, deps: Deps, acc: _Acc, *, source: str, full_duration: float | None = None
) -> None:
    settings = deps.settings
    with _Timer(acc, "ffprobe"):
        try:
            info = await ffmpeg.probe(path)
        except ffmpeg.MediaToolError as exc:
            log.warning("ffprobe_failed", error=str(exc))
            return
    if not info.has_video and not info.has_audio:
        return
    acc.method("ffmpeg")
    acc.video_path = str(path) if info.has_video else None
    acc.duration_s = full_duration or info.duration_s

    plan = ffmpeg.plan_frame_times(info.duration_s, settings.frame_fps, settings.frame_cap)
    frames_task = (
        ffmpeg.extract_frames(path, plan.times, acc.media_dir / "frames", duration_s=info.duration_s)
        if info.has_video
        else _none()
    )
    audio_task = (
        ffmpeg.extract_audio(path, acc.media_dir / "audio.wav", settings.audio_cap_s)
        if info.has_audio
        else _none()
    )
    with _Timer(acc, "ffmpeg"):
        frames, audio = await asyncio.gather(frames_task, audio_task, return_exceptions=True)
    if isinstance(audio, Path):
        acc.audio_path = str(audio)
    elif isinstance(audio, BaseException):
        log.warning("audio_extract_failed", error=str(audio))
    if isinstance(frames, BaseException):
        log.warning("frame_extract_failed", error=str(frames))
        return
    if not frames:
        return
    with _Timer(acc, "hash_frames"):
        acc.media.extend(
            await describe_parallel(
                [(p, t) for t, p in frames], MediaKind.VIDEO_FRAME, source, len(acc.media)
            )
        )
    truncated = bool(full_duration and info.duration_s and full_duration > info.duration_s + 1)
    note = plan.note(len(frames))
    if truncated:
        note = (
            f"Sampled {len(frames)} frames of the first {info.duration_s:.0f} s "
            f"of a {full_duration:.0f} s video"
        )
    capped = plan.capped or truncated
    acc.sampling = SamplingInfo(
        fps=round(plan.effective_fps, 4),
        frames=len(frames),
        duration_s=acc.duration_s,
        capped=capped,
        note=note,
    )


async def _none() -> None:
    return None


async def describe_parallel(
    items: list[tuple[Path, float | None]], kind: MediaKind, source: str, start_index: int = 0
) -> list[MediaAsset]:
    """Hash frames on the CPU pool in chunks (PIL decode releases the GIL), ids stay in order."""
    chunks = [items[i::4] for i in range(4)]
    parts = await asyncio.gather(*(run_cpu(_describe_assets, c, kind, source, 0) for c in chunks if c))
    by_path = {a.path: a for part in parts for a in part}
    ordered = [by_path[str(p)] for p, _ in items if str(p) in by_path]
    return [a.model_copy(update={"id": f"M{start_index + i + 1}"}) for i, a in enumerate(ordered)]


def _describe_assets(
    items: list[tuple[Path, float | None]], kind: MediaKind, source: str, start_index: int = 0
) -> list[MediaAsset]:
    """sha256 + pHash + size for each file; ids M1.. continue after existing assets."""
    out: list[MediaAsset] = []
    for path, ts in items:
        info = describe_image(path)
        out.append(
            MediaAsset(
                id=f"M{start_index + len(out) + 1}",
                kind=kind,
                path=str(path),
                sha256=info.sha256,
                phash=info.phash,
                frame_ts=float(ts) if ts is not None else None,
                width=info.width,
                height=info.height,
                source=source,
            )
        )
    return out
