"""On-screen text with EasyOCR (English, plus Hindi when the content points to Devanagari), loaded once.

Keyframes are chosen for diversity (pHash distance) so OCR passes are not spent on copies of the same
caption overlay. Measured on this laptop (2026-10-31, 640 px frames, CPU under load): the English
reader takes ~3 s per frame and the English+Hindi (Devanagari) reader ~8 s, and 960 px costs ~2x
640 px. So frames are downscaled to 640 px (overlay text is large), the Devanagari reader is used only
when the caption/title/locale contains or asks for Hindi, and OCR stops starting new frames once a
time budget is spent (always at least one frame) so the video path stays near its latency target.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from rapidfuzz import fuzz

from veriloop.contracts import MediaAsset
from veriloop.core.config import Settings
from veriloop.services.ingestion.imaging import hamming, load_rgb, resized

log = structlog.get_logger(__name__)

LANGS_EN: tuple[str, ...] = ("en",)
LANGS_EN_HI: tuple[str, ...] = ("en", "hi")
OCR_MAX_SIDE = 640
OCR_BUDGET_S = 6.0
MIN_CONFIDENCE = 0.35
MAX_KEYFRAMES = 4
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

_readers: dict[tuple[str, ...], Any] = {}
_errors: dict[tuple[str, ...], str] = {}
_lock = threading.Lock()
_infer_lock = threading.Lock()  # one OCR pass at a time: readers are shared and torch is multi-threaded


def choose_langs(*hints: str | None, locale: str | None = None) -> tuple[str, ...]:
    """en+hi when any hint text has Devanagari or the locale is Hindi/Marathi/Nepali; else English only."""
    if locale and locale.lower().split("-")[0] in {"hi", "mr", "ne"}:
        return LANGS_EN_HI
    return LANGS_EN_HI if any(h and _DEVANAGARI.search(h) for h in hints) else LANGS_EN


def pick_keyframes(
    assets: list[MediaAsset], limit: int = MAX_KEYFRAMES, min_distance: int = 8
) -> list[MediaAsset]:
    """Evenly spread, visually distinct frames/images (page captures last, only if nothing else)."""
    primary = [a for a in assets if a.source != "page_capture"]
    pool = primary or list(assets)
    if len(pool) <= limit:
        return pool
    step = len(pool) / limit
    candidates = [pool[int(i * step + step / 2)] for i in range(limit)] + pool
    chosen: list[MediaAsset] = []
    for asset in candidates:
        if asset in chosen:
            continue
        if asset.phash and any(c.phash and hamming(asset.phash, c.phash) < min_distance for c in chosen):
            continue
        chosen.append(asset)
        if len(chosen) == limit:
            break
    return sorted(chosen, key=lambda a: a.frame_ts or 0.0)


def _get_reader(settings: Settings, langs: tuple[str, ...] = LANGS_EN_HI) -> Any:
    with _lock:
        if langs in _readers:
            return _readers[langs]
        if langs in _errors:
            raise RuntimeError(_errors[langs])
        started = time.perf_counter()
        try:
            import easyocr

            root = settings.models_dir / "easyocr"
            root.mkdir(parents=True, exist_ok=True)
            reader = easyocr.Reader(
                list(langs),
                gpu=False,
                model_storage_directory=str(root),
                user_network_directory=str(root / "user_network"),
                download_enabled=True,
                verbose=False,
            )
        except Exception as exc:
            _errors[langs] = f"easyocr {'+'.join(langs)} unavailable: {type(exc).__name__}: {exc}"
            log.warning("ocr_model_unavailable", langs=langs, error=_errors[langs])
            raise RuntimeError(_errors[langs]) from exc
        _readers[langs] = reader
        log.info("ocr_model_loaded", langs=langs, load_ms=int((time.perf_counter() - started) * 1000))
        return reader


def load_blocking(settings: Settings) -> None:
    for langs in (LANGS_EN, LANGS_EN_HI):
        try:
            _get_reader(settings, langs)
        except RuntimeError:
            pass


def ocr_blocking(
    paths: list[str | Path],
    settings: Settings,
    langs: tuple[str, ...] = LANGS_EN,
    budget_s: float = OCR_BUDGET_S,
) -> list[str]:
    """Unique text lines (reading order per frame), near-duplicates across frames removed.

    Frames are read in the given order until `budget_s` is spent (the first frame always runs).
    """
    reader = _get_reader(settings, langs)
    lines: list[str] = []
    started = time.perf_counter()
    for i, path in enumerate(paths):
        if i > 0 and time.perf_counter() - started >= budget_s:
            log.info("ocr_budget_reached", frames_read=i, frames_total=len(paths), budget_s=budget_s)
            break
        try:
            img = np.asarray(resized(load_rgb(path), OCR_MAX_SIDE))
        except ValueError:
            continue
        with _infer_lock:
            results = reader.readtext(img, detail=1, paragraph=False, batch_size=32)
        # Sort boxes top-to-bottom, left-to-right so multi-box captions read naturally.
        results = sorted(
            results, key=lambda r: (round(min(p[1] for p in r[0]) / 20), min(p[0] for p in r[0]))
        )
        for _box, text, conf in results:
            text = " ".join(str(text).split())
            if conf < MIN_CONFIDENCE or len(text) < 2:
                continue
            if any(fuzz.ratio(text.lower(), seen.lower()) >= 88 for seen in lines):
                continue
            lines.append(text)
    return lines


def status() -> dict[str, str]:
    out = {f"easyocr/{'+'.join(k)}": "loaded" for k in _readers}
    out.update({f"easyocr/{'+'.join(k)}": "unavailable" for k in _errors})
    return out or {"easyocr": "idle"}
