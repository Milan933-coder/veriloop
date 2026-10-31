"""Ingestion service: URL / upload / text -> ResolvedInput (BUILD_SPEC §5)."""

from __future__ import annotations

from veriloop.deps import Deps

from .service import resolve_input

__all__ = ["prewarm", "resolve_input"]


async def prewarm(deps: Deps) -> None:
    """Nothing heavy to load: ffmpeg/yt-dlp are external and stateless."""
    return None
