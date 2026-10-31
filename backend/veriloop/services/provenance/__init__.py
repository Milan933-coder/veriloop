"""Provenance service: own pHash/CLIP index + keyed reverse-image tools -> first-seen (BUILD_SPEC §5)."""

from __future__ import annotations

import structlog

from veriloop.core.executor import run_cpu
from veriloop.deps import Deps

from . import clip
from .service import run_provenance

__all__ = ["prewarm", "run_provenance", "status"]

log = structlog.get_logger(__name__)


async def prewarm(deps: Deps) -> None:
    """Load CLIP once (weights cached under models_dir/open_clip)."""
    if deps.settings.ml_enable:
        await run_cpu(clip.load_blocking, deps.settings)
        log.info("provenance_prewarmed", **status())


def status() -> dict[str, str]:
    """Local model states for /health.ml."""
    return clip.status()
