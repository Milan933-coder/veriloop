"""yt-dlp download with size/duration caps, run as ToolClient tool `ytdlp`.

yt-dlp runs in-process in a worker thread (metadata + download in ONE extraction: the info dict is
reused for the download, saving a second round of page requests). It is I/O-bound, so it uses
`asyncio.to_thread` rather than the small CPU pool that local models need. Cancellation (ToolClient's
per-attempt timeout) sets an Event that the progress hook checks, which aborts the transfer.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any

import structlog

from veriloop.core.errors import ToolAttemptError

log = structlog.get_logger(__name__)

# Metadata copied into the tool payload (and therefore into cassettes): small, JSON-safe fields only.
INFO_FIELDS = (
    "id",
    "title",
    "description",
    "uploader",
    "uploader_id",
    "channel",
    "upload_date",
    "timestamp",
    "duration",
    "width",
    "height",
    "ext",
    "webpage_url",
    "extractor_key",
    "thumbnail",
    "language",
    "view_count",
)

_LOGIN = re.compile(
    r"(log ?in|sign in|cookies|authenticat|private (video|account|post)|age.restricted|"
    r"members.only|rate.limit reached or login required|this content isn.t available)",
    re.IGNORECASE,
)
_NO_MEDIA = re.compile(
    r"(no video could be found|there.s no video|no media found|does not contain (any )?video|"
    r"no video formats found|requested format is not available)",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(r"unsupported url", re.IGNORECASE)
_NOT_FOUND = re.compile(
    r"(http error 404|video unavailable|has been removed|not available|does not exist)", re.I
)
_TRANSIENT = re.compile(r"(timed out|timeout|connection|temporarily|http error 5\d\d|reset by peer)", re.I)


class _Cancelled(Exception):
    pass


class _Logger:
    """Collect yt-dlp messages instead of printing them; errors become the classified message."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("ytdlp_warning", message=str(msg)[:300])

    def error(self, msg: str) -> None:
        self.errors.append(str(msg))


def classify_error(message: str) -> ToolAttemptError:
    msg = " ".join(message.split())[:400]
    if _LOGIN.search(msg):
        return ToolAttemptError("login_required", msg)
    if _NO_MEDIA.search(msg):
        return ToolAttemptError("no_media", msg)
    if _UNSUPPORTED.search(msg):
        return ToolAttemptError("unsupported", msg)
    if _NOT_FOUND.search(msg):
        return ToolAttemptError("not_found", msg)
    if _TRANSIENT.search(msg):
        return ToolAttemptError("http_error", msg, retryable=True)
    return ToolAttemptError("exception", msg)


def _first_entry(info: dict[str, Any]) -> dict[str, Any]:
    # Multi-video posts come back as playlists; the first video is the one shown first in the post.
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
        if not entries:
            raise ToolAttemptError("no_media", "post contains no downloadable video")
        return entries[0]
    return info


def download_blocking(
    url: str,
    out_dir: str | Path,
    *,
    max_bytes: int,
    max_video_s: int,
    max_res: int,
    cancel: threading.Event,
) -> dict[str, Any]:
    """Extract metadata, then download (capped). Raises ToolAttemptError with a classified type."""
    import yt_dlp
    from yt_dlp.utils import DownloadError, download_range_func

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = _Logger()

    def hook(status: dict[str, Any]) -> None:
        if cancel.is_set():
            raise _Cancelled("download cancelled (tool timeout)")
        done = status.get("downloaded_bytes") or 0
        if done > max_bytes:
            raise ToolAttemptError("too_large", f"download exceeded MAX_UPLOAD_MB ({max_bytes // 2**20} MB)")

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": logger,
        "cachedir": False,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "paths": {"home": str(out_dir), "temp": str(out_dir)},
        # Smallest-dimension cap (vertical shorts too), prefer mp4/m4a so the merge is a cheap remux.
        "format_sort": [f"res:{max_res}", "ext:mp4:m4a", "size"],
        "merge_output_format": "mp4",
        "max_filesize": max_bytes,
        "progress_hooks": [hook],
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = _first_entry(ydl.extract_info(url, download=False) or {})
            duration = info.get("duration")
            partial = False
            if isinstance(duration, int | float) and duration > max_video_s:
                # E8: never fetch an unbounded video; the first MAX_VIDEO_S seconds are sampled.
                ydl.params["download_ranges"] = download_range_func(None, [(0, max_video_s)])
                partial = True
            if cancel.is_set():
                raise _Cancelled("download cancelled (tool timeout)")
            result = ydl.process_ie_result(info, download=True) or info
    except _Cancelled as exc:
        raise ToolAttemptError("timeout", str(exc), retryable=False) from exc
    except ToolAttemptError:
        raise
    except DownloadError as exc:
        raise classify_error(" | ".join(logger.errors) or str(exc)) from exc
    except Exception as exc:  # extractor bugs surface as arbitrary exceptions
        raise classify_error(f"{type(exc).__name__}: {exc}") from exc

    path: str | None = None
    for item in result.get("requested_downloads") or []:
        candidate = item.get("filepath") or item.get("_filename")
        if candidate and Path(candidate).exists():
            path = str(candidate)
    if path is None:
        found = sorted(p for p in out_dir.glob("source.*") if p.suffix not in {".part", ".ytdl"})
        path = str(found[0]) if found else None
    if path is None:
        hint = " | ".join(logger.errors)
        if "larger than max-filesize" in hint.lower() or not hint:
            raise ToolAttemptError("too_large", hint or "file larger than MAX_UPLOAD_MB; not downloaded")
        raise classify_error(hint)
    meta = {k: result.get(k, info.get(k)) for k in INFO_FIELDS}
    return {"info": meta, "path": path, "bytes": Path(path).stat().st_size, "partial": partial}


async def download(
    url: str, out_dir: str | Path, *, max_bytes: int, max_video_s: int, max_res: int = 720
) -> dict[str, Any]:
    cancel = threading.Event()
    try:
        return await asyncio.to_thread(
            download_blocking,
            url,
            out_dir,
            max_bytes=max_bytes,
            max_video_s=max_video_s,
            max_res=max_res,
            cancel=cancel,
        )
    except asyncio.CancelledError:
        cancel.set()  # the worker thread aborts at its next progress callback
        raise
