"""Forensic service: face/AI-generated detectors ensemble with an explicit abstain band (BUILD_SPEC §5)."""

from __future__ import annotations

import structlog

from veriloop.core.executor import run_cpu
from veriloop.deps import Deps

from . import classifiers, faces
from .models_config import LOCAL_MODELS
from .service import run_forensics

__all__ = ["prewarm", "run_forensics", "status"]

log = structlog.get_logger(__name__)


async def prewarm(deps: Deps) -> None:
    """Download YuNet and load the pinned HF detectors once (weights cached under models_dir)."""
    if not deps.settings.ml_enable:
        return
    await faces.ensure_yunet(deps.settings, deps.tools)
    for spec in LOCAL_MODELS:
        await run_cpu(classifiers.get_classifier(spec, deps.settings).load)
    log.info("forensic_prewarmed", **status())


def status() -> dict[str, str]:
    """Local model states for /health.ml."""
    return classifiers.status()
