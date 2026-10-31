"""ffprobe / ffmpeg wrappers: probe, frame sampling plan, frame + audio extraction.

Subprocesses run through `subprocess.run(timeout=...)` in a worker thread rather than asyncio's
subprocess API: uvicorn on Windows may run a selector event loop, which cannot spawn subprocesses,
and `subprocess.run` kills the child on timeout, so a hung ffmpeg can never wedge a job.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
MAX_FRAME_SIDE = 1280  # long side of sampled frames; enough for faces/OCR, keeps CPU models fast


class MediaToolError(RuntimeError):
    """ffmpeg/ffprobe missing, failed or timed out."""


@dataclass
class ProbeInfo:
    duration_s: float | None = None
    has_video: bool = False
    has_audio: bool = False
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    format_name: str | None = None
    bit_rate: int | None = None
    nb_frames: int | None = None
    tags: dict[str, str] = field(default_factory=dict)  # container tags (encoder, major_brand, ...)
    video_tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_still_image(self) -> bool:
        # ffprobe reports images as a single-frame "video" stream with an image codec.
        return (
            self.has_video and (self.format_name or "").endswith(("_pipe", "image2")) and not self.has_audio
        )


def binary(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            args, capture_output=True, timeout=timeout_s, check=False, creationflags=_NO_WINDOW
        )
    except FileNotFoundError as exc:
        raise MediaToolError(f"{args[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"{Path(args[0]).name} timed out after {timeout_s:g}s") from exc


def _fps(rate: str | None) -> float | None:
    if not rate or rate in {"0/0", "N/A"}:
        return None
    try:
        num, _, den = rate.partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return value if 0 < value < 1000 else None


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0 else None


def parse_probe(data: dict[str, Any]) -> ProbeInfo:
    fmt = data.get("format") or {}
    info = ProbeInfo(
        duration_s=_float(fmt.get("duration")),
        format_name=fmt.get("format_name"),
        bit_rate=int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
        tags={str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()},
    )
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if (
            kind == "video"
            and not info.has_video
            and not (stream.get("disposition") or {}).get("attached_pic")
        ):
            info.has_video = True
            info.width, info.height = stream.get("width"), stream.get("height")
            info.video_codec = stream.get("codec_name")
            info.fps = _fps(stream.get("avg_frame_rate")) or _fps(stream.get("r_frame_rate"))
            nb = stream.get("nb_frames")
            info.nb_frames = int(nb) if str(nb or "").isdigit() else None
            info.video_tags = {str(k).lower(): str(v) for k, v in (stream.get("tags") or {}).items()}
            if info.duration_s is None:
                info.duration_s = _float(stream.get("duration"))
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = stream.get("codec_name")
    return info


def probe_blocking(path: str | Path, timeout_s: float = 20.0) -> ProbeInfo:
    exe = binary("ffprobe")
    if exe is None:
        raise MediaToolError("ffprobe not found on PATH")
    proc = run_cmd(
        [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout_s
    )
    if proc.returncode != 0:
        raise MediaToolError(f"ffprobe failed: {proc.stderr.decode(errors='replace')[:300]}")
    try:
        return parse_probe(json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}"))
    except ValueError as exc:
        raise MediaToolError(f"ffprobe returned invalid JSON: {exc}") from exc


async def probe(path: str | Path, timeout_s: float = 20.0) -> ProbeInfo:
    return await asyncio.to_thread(probe_blocking, path, timeout_s)


@dataclass(frozen=True)
class FramePlan:
    times: list[float]
    effective_fps: float
    capped: bool
    duration_s: float | None

    def note(self, frames: int | None = None) -> str:
        n = len(self.times) if frames is None else frames
        if self.duration_s is None:
            return f"Sampled {n} frame{'s' if n != 1 else ''} (duration unknown)"
        return f"Sampled {n} frame{'s' if n != 1 else ''} of {self.duration_s:.0f} s video"


def plan_frame_times(duration_s: float | None, fps: float, cap: int) -> FramePlan:
    """Timestamps to sample: `fps` across the video, or `cap` frames spread evenly when longer (E8).

    Samples sit at the centre of each interval (never t=0, which is often a black/title frame).
    """
    cap = max(1, cap)
    fps = fps if fps > 0 else 1.0
    if not duration_s or duration_s <= 0:
        return FramePlan([0.0], fps, False, duration_s)
    natural = max(1, math.floor(duration_s * fps))
    if natural <= cap:
        times = [min((i + 0.5) / fps, max(0.0, duration_s - 0.05)) for i in range(natural)]
        return FramePlan([round(t, 3) for t in times], fps, False, duration_s)
    step = duration_s / cap
    times = [round((i + 0.5) * step, 3) for i in range(cap)]
    return FramePlan(times, cap / duration_s, True, duration_s)


_SCALE = f"scale=w='trunc(min(1,{MAX_FRAME_SIDE}/max(iw,ih))*iw/2)*2':h=-2"
# Below this length one decoding pass with the fps filter beats N keyframe seeks (measured: 34 s
# 720p h264 -> 16 frames in 0.97 s single-pass vs 2.4 s multi-seek); above it, seeks win (E8).
SINGLE_PASS_MAX_S = 120.0


def _seek_cmd(exe: str, src: Path, t: float, out: Path) -> list[str]:
    return [
        exe, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src),
        "-frames:v", "1", "-vf", _SCALE, "-q:v", "2", str(out),
    ]  # fmt: skip


def _single_pass(exe: str, src: Path, times: list[float], out_dir: Path, timeout_s: float) -> bool:
    """Evenly spaced times -> seek to the first, then fps=1/step for exactly len(times) frames."""
    step = times[1] - times[0] if len(times) > 1 else 1.0
    pattern = out_dir / "frame_%03d.jpg"
    args = [
        exe, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{times[0]:.3f}", "-i", str(src),
        "-vf", f"fps=1/{step:.6f},{_SCALE}", "-frames:v", str(len(times)), "-q:v", "2",
        "-start_number", "0", str(pattern),
    ]  # fmt: skip
    proc = run_cmd(args, timeout_s)
    if proc.returncode != 0:
        log.info("ffmpeg_single_pass_failed", stderr=proc.stderr.decode(errors="replace")[:200])
    return proc.returncode == 0


def extract_frames_blocking(
    src: str | Path,
    times: list[float],
    out_dir: str | Path,
    timeout_s: float = 60.0,
    *,
    duration_s: float | None = None,
) -> list[tuple[float, Path]]:
    """Write frame_000.jpg.. at `times` (long side <= MAX_FRAME_SIDE). Returns (t, path) that exist."""
    exe = binary("ffmpeg")
    if exe is None:
        raise MediaToolError("ffmpeg not found on PATH")
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    items = [(t, out_dir / f"frame_{i:03d}.jpg") for i, t in enumerate(times)]
    if duration_s is not None and duration_s <= SINGLE_PASS_MAX_S and len(times) > 1:
        _single_pass(exe, src, times, out_dir, timeout_s)
    missing = [(t, p) for t, p in items if not p.exists()]
    if missing:
        # Long videos (or a failed single pass): one input-seeking process per frame, 4 at a time.
        per_frame = max(5.0, timeout_s / 2)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda tp: run_cmd(_seek_cmd(exe, src, tp[0], tp[1]), per_frame), missing))
    return [(t, p) for t, p in items if p.exists() and p.stat().st_size > 0]


async def extract_frames(
    src: str | Path,
    times: list[float],
    out_dir: str | Path,
    timeout_s: float = 60.0,
    *,
    duration_s: float | None = None,
) -> list[tuple[float, Path]]:
    return await asyncio.to_thread(
        extract_frames_blocking, src, times, out_dir, timeout_s, duration_s=duration_s
    )


def extract_audio_blocking(src: str | Path, out: str | Path, cap_s: float, timeout_s: float = 60.0) -> Path:
    """Mono 16 kHz PCM WAV of the first `cap_s` seconds (Whisper's native input format)."""
    exe = binary("ffmpeg")
    if exe is None:
        raise MediaToolError("ffmpeg not found on PATH")
    out = Path(out)
    args = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
    ]
    args += ["-t", f"{cap_s:g}", "-c:a", "pcm_s16le", str(out)]
    proc = run_cmd(args, timeout_s)
    if proc.returncode != 0 or not out.exists():
        raise MediaToolError(f"audio extraction failed: {proc.stderr.decode(errors='replace')[:300]}")
    return out


async def extract_audio(src: str | Path, out: str | Path, cap_s: float, timeout_s: float = 60.0) -> Path:
    return await asyncio.to_thread(extract_audio_blocking, src, out, cap_s, timeout_s)
