"""Synthetic media for the media-package tests (no network, no model downloads).

Images are textured noise (informative for pHash, never "flat"); videos come from ffmpeg's lavfi test
sources, so the real ffprobe/ffmpeg pipeline is exercised offline.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from veriloop.core.ids import new_job_id
from veriloop.db import repo

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not on PATH"
)


def textured(size: tuple[int, int] = (320, 240), seed: int = 0) -> Image.Image:
    rng = np.random.default_rng(seed)
    w, h = size
    # Low-frequency structure (so pHash is stable) plus noise (so the image is never flat).
    base = rng.integers(0, 255, (max(2, h // 40), max(2, w // 40), 3), dtype=np.uint8)
    img = Image.fromarray(base).resize((w, h), Image.Resampling.BICUBIC)
    noise = rng.integers(-20, 20, (h, w, 3))
    arr = np.clip(np.asarray(img, dtype=np.int16) + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def make_image(path: Path, size: tuple[int, int] = (320, 240), seed: int = 0, quality: int = 92) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = textured(size, seed)
    if path.suffix.lower() in (".jpg", ".jpeg"):
        img.save(path, format="JPEG", quality=quality)
    else:
        img.save(path)
    return path


def make_video(path: Path, seconds: float, *, size: str = "160x120", audio: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    args += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=10:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True, timeout=60)
    return path


async def new_job(db: object, url: str | None = None) -> str:
    job_id = new_job_id()
    await repo.create_job(db, job_id, request={"url": url} if url else {"text": "test"})  # type: ignore[arg-type]
    return job_id
