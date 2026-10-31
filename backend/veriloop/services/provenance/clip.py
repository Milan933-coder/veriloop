"""CLIP image embeddings (open_clip ViT-B-32, LAION-2B weights) for the semantic near-duplicate index.

512-d, L2-normalised float32, so pgvector cosine (`<=>`) works directly. Loaded once per process;
the seed script and the provenance service share this module so index and query vectors match.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from PIL import Image

from veriloop.core.config import Settings
from veriloop.services.ingestion.imaging import load_rgb

log = structlog.get_logger(__name__)

ARCH = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"  # open_clip tag -> HF laion/CLIP-ViT-B-32-laion2B-s34B-b79K
MODEL_ID = f"open_clip/{ARCH}/{PRETRAINED}"
DIM = 512
BATCH = 16

_model: Any = None
_preprocess: Any = None
_error: str | None = None
_load_lock = threading.Lock()
_infer_lock = threading.Lock()


def load_blocking(settings: Settings) -> bool:
    global _model, _preprocess, _error
    with _load_lock:
        if _model is not None:
            return True
        if _error is not None:
            return False
        started = time.perf_counter()
        try:
            import open_clip

            model, _, preprocess = open_clip.create_model_and_transforms(
                ARCH, pretrained=PRETRAINED, cache_dir=str(settings.models_dir / "open_clip")
            )
            model.eval()
        except Exception as exc:
            _error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("clip_unavailable", model=MODEL_ID, error=_error)
            return False
        _model, _preprocess = model, preprocess
        log.info("clip_loaded", model=MODEL_ID, load_ms=int((time.perf_counter() - started) * 1000))
        return True


def available() -> bool:
    return _error is None


def embed_images_blocking(images: list[Image.Image], settings: Settings) -> np.ndarray:
    if not images:
        return np.zeros((0, DIM), dtype=np.float32)
    if not load_blocking(settings):
        raise RuntimeError(_error or "CLIP unavailable")
    import torch

    chunks: list[np.ndarray] = []
    with _infer_lock, torch.inference_mode():
        for start in range(0, len(images), BATCH):
            batch = torch.stack([_preprocess(im.convert("RGB")) for im in images[start : start + BATCH]])
            feats = _model.encode_image(batch).float()
            feats = feats / feats.norm(dim=-1, keepdim=True).clamp_min(1e-12)
            chunks.append(feats.cpu().numpy().astype(np.float32))
    return np.concatenate(chunks, axis=0)


def embed_paths_blocking(paths: list[str | Path], settings: Settings) -> np.ndarray:
    return embed_images_blocking([load_rgb(p) for p in paths], settings)


def status() -> dict[str, str]:
    if _model is not None:
        return {MODEL_ID: "loaded"}
    return {MODEL_ID: "unavailable" if _error else "idle"}
