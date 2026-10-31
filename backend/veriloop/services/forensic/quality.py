"""Media-quality gates for E7 (adversarially / heavily recompressed or tiny media -> inconclusive).

Detectors learn compression artefacts as much as manipulation artefacts, so their scores on
heavily recompressed or tiny media are not trustworthy in either direction.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

MIN_IMAGE_SIDE = 160
MIN_JPEG_QUALITY = 50
MIN_VIDEO_BPP = 0.015  # bits per pixel per frame; social 720p is ~0.05, a crushed re-upload ~0.005
MIN_VIDEO_SIDE = 240

# IJG standard luminance quantisation table (quality 50), natural order.
_STD_LUMA = np.array(
    [
        16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
        14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
        18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
        49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
    ],
    dtype=np.float64,
)  # fmt: skip


def jpeg_quality(path: str | Path) -> int | None:
    """Estimated IJG quality (1-100) from the luminance quantisation table; None if not a JPEG.

    PIL exposes tables in zigzag order and the standard table above is in natural order, but the
    estimate only uses the mean ratio, which is order-independent.
    """
    try:
        with Image.open(path) as im:
            if im.format != "JPEG":
                return None
            tables = getattr(im, "quantization", None) or {}
    except OSError:
        return None
    luma = tables.get(0)
    if luma is None or len(luma) != 64:
        return None
    scale = float(np.mean(np.asarray(luma, dtype=np.float64) / _STD_LUMA) * 100.0)
    if scale <= 0:
        return 100
    quality = (200.0 - scale) / 2.0 if scale <= 100.0 else 5000.0 / scale
    return round(max(1.0, min(100.0, quality)))


def image_issue(path: str | Path, width: int | None, height: int | None) -> str | None:
    if width and height and min(width, height) < MIN_IMAGE_SIDE:
        return f"image too small for reliable forensics ({width}x{height} px)"
    q = jpeg_quality(path)
    if q is not None and q < MIN_JPEG_QUALITY:
        return f"heavily recompressed JPEG (estimated quality {q})"
    return None


def video_issue(bit_rate: int | None, width: int | None, height: int | None, fps: float | None) -> str | None:
    if width and height and min(width, height) < MIN_VIDEO_SIDE:
        return f"video resolution too low for reliable forensics ({width}x{height})"
    if bit_rate and width and height and fps:
        bpp = bit_rate / float(width * height * fps)
        if bpp < MIN_VIDEO_BPP:
            return f"heavily compressed video ({bpp:.3f} bits/pixel/frame)"
    return None
