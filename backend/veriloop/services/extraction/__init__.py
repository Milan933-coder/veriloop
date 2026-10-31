"""Extraction service: ASR + OCR + caption -> atomic claims, worthiness, subject (BUILD_SPEC §5)."""

from __future__ import annotations

import structlog

from veriloop.core.executor import run_cpu
from veriloop.deps import Deps

from . import asr, ocr
from .service import VISUAL_QUERY_PREFIX, extract_claims, visual_queries

__all__ = ["VISUAL_QUERY_PREFIX", "extract_claims", "prewarm", "status", "visual_queries"]

log = structlog.get_logger(__name__)


async def prewarm(deps: Deps) -> None:
    """Load Whisper (ASR_MODEL + base) and EasyOCR once, downloading weights into models_dir."""
    if not deps.settings.ml_enable:
        return
    await run_cpu(asr.load_blocking, deps.settings)
    await run_cpu(ocr.load_blocking, deps.settings)
    log.info("extraction_prewarmed", **status())


def status() -> dict[str, str]:
    """Local model states for /health.ml."""
    return {**asr.status(), **ocr.status()}
