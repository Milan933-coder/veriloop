"""Face detection + margin crops for the face-deepfake detectors.

YuNet (OpenCV zoo ONNX, ~230 KB, downloaded once into models_dir/opencv) is preferred: it returns a
confidence and handles profile/rotated faces far better than Haar. The bundled Haar cascade is the
zero-download fallback. Crops keep a margin because face-swap artefacts sit at the blending boundary
(FaceForensics++-style detectors are trained on ~1.3x crops).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import structlog
from PIL import Image

from veriloop.core.config import Settings
from veriloop.core.http import raise_for_status
from veriloop.core.tool_client import ToolClient

log = structlog.get_logger(__name__)

YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
DETECT_MAX_SIDE = 960
MIN_FACE_PX = 64  # below this the classifiers see mush; such faces are counted but not scored
MARGIN = 0.3  # each side, relative to the face box
MAX_FACES_PER_FRAME = 2
NO_DETECTOR = "none"

_lock = threading.RLock()
_yunet: object | None = None
_haar: object | None = None
_backend: str | None = None


@dataclass
class FaceCrop:
    image: Image.Image
    box: tuple[int, int, int, int]  # x, y, w, h in the original frame
    score: float
    tiny: bool


def yunet_path(settings: Settings) -> Path:
    return settings.models_dir / "opencv" / YUNET_FILE


def yunet_ready(settings: Settings) -> bool:
    return _yunet_ready(yunet_path(settings))


def _yunet_ready(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 100_000


async def ensure_yunet(settings: Settings, tools: ToolClient) -> Path | None:
    """Download YuNet once (prewarm) through ToolClient. Returns the path or None (Haar fallback)."""
    path = yunet_path(settings)
    if await asyncio.to_thread(_yunet_ready, path):
        return path

    async def fn() -> dict[str, Any]:
        resp = raise_for_status(await tools.http.get(YUNET_URL, follow_redirects=True))
        tmp = path.with_suffix(".part")

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(resp.content)
            tmp.replace(path)

        await asyncio.to_thread(write)
        return {"path": str(path), "bytes": len(resp.content)}

    # Weights are a file on disk, not a cacheable payload: cache_ttl_s=0 so a deleted file re-downloads.
    res = await tools.call(
        "page_fetch", "model_download", {"url": YUNET_URL}, fn, cache_ttl_s=0, timeout_s=60
    )
    if not res.ok:
        log.warning("yunet_download_failed", error=res.error.message if res.error else "unknown")
        return None
    _reset_backend()  # pick YuNet up on the next detection instead of the Haar fallback
    return path if await asyncio.to_thread(_yunet_ready, path) else None


def _reset_backend() -> None:
    global _yunet, _haar, _backend
    with _lock:
        _yunet = _haar = _backend = None


def backend(settings: Settings) -> str:
    with _lock:
        _init(settings)
        return _backend or "none"


def _init(settings: Settings) -> None:
    """Pick the detector once (caller holds _lock)."""
    global _yunet, _haar, _backend
    if _backend is not None:
        return
    path = yunet_path(settings)
    if path.exists() and hasattr(cv2, "FaceDetectorYN"):
        try:
            _yunet = cv2.FaceDetectorYN.create(str(path), "", (320, 320), 0.75, 0.3, 50)
            _backend = "yunet-2023mar"
            return
        except cv2.error as exc:
            log.warning("yunet_load_failed", error=str(exc))
    # OpenCV 5 moved Haar cascades out of the main package: then there is no zero-download fallback,
    # faces are not detected and every frame goes to the whole-image detector (reported via backend()).
    cascade = getattr(cv2, "CascadeClassifier", None)
    haar_file = (
        Path(getattr(getattr(cv2, "data", None), "haarcascades", "")) / "haarcascade_frontalface_default.xml"
    )
    if cascade is not None and haar_file.exists():
        _haar = cascade(str(haar_file))
        _backend = "haar-frontalface-default"
    else:
        _backend = NO_DETECTOR
        log.warning(
            "face_detector_unavailable", reason="YuNet not downloaded and no Haar cascade in this OpenCV"
        )


def _detect_boxes(bgr: np.ndarray) -> list[tuple[int, int, int, int, float]]:
    h, w = bgr.shape[:2]
    scale = min(1.0, DETECT_MAX_SIDE / max(h, w))
    small = cv2.resize(bgr, (int(w * scale), int(h * scale))) if scale < 1.0 else bgr
    boxes: list[tuple[int, int, int, int, float]] = []
    if _yunet is not None:
        _yunet.setInputSize((small.shape[1], small.shape[0]))  # type: ignore[attr-defined]
        _, faces = _yunet.detect(small)  # type: ignore[attr-defined]
        for row in faces if faces is not None else []:
            x, y, bw, bh = (float(v) / scale for v in row[:4])
            boxes.append((int(x), int(y), int(bw), int(bh), float(row[-1])))
    elif _haar is not None:
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        min_side = max(24, int(min(small.shape[:2]) * 0.05))
        found = _haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side))  # type: ignore[attr-defined]
        for x, y, bw, bh in found if len(found) else []:
            boxes.append((int(x / scale), int(y / scale), int(bw / scale), int(bh / scale), 0.8))
    return boxes


def crop_with_margin(img: Image.Image, box: tuple[int, int, int, int], margin: float = MARGIN) -> Image.Image:
    """Square crop centred on the face, enlarged by `margin` per side, clipped to the image."""
    x, y, w, h = box
    side = max(w, h) * (1 + 2 * margin)
    cx, cy = x + w / 2, y + h / 2
    left, top = max(0, int(cx - side / 2)), max(0, int(cy - side / 2))
    right, bottom = min(img.width, int(cx + side / 2)), min(img.height, int(cy + side / 2))
    return img.crop((left, top, right, bottom))


def detect_faces(img: Image.Image, settings: Settings) -> list[FaceCrop]:
    """Up to MAX_FACES_PER_FRAME largest confident faces, cropped with margin from the full frame."""
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    with _lock:  # FaceDetectorYN keeps per-instance input size state
        _init(settings)
        boxes = _detect_boxes(bgr)
    boxes.sort(key=lambda b: b[2] * b[3] * b[4], reverse=True)
    crops: list[FaceCrop] = []
    for x, y, w, h, score in boxes[:MAX_FACES_PER_FRAME]:
        if w <= 0 or h <= 0:
            continue
        crops.append(
            FaceCrop(
                image=crop_with_margin(img, (x, y, w, h)),
                box=(x, y, w, h),
                score=round(score, 3),
                tiny=min(w, h) < MIN_FACE_PX,
            )
        )
    return crops
