"""SMOKE (network + ml): demo case (b) YouTube short end to end: resolve -> frames/audio -> ASR/OCR -> claims.

    cd backend && $PY -m pytest tests/services/ingestion/test_smoke_video.py -m "network and ml" -s

Uses the real .env (VERILOOP_HOME on D:, aicredits key), real yt-dlp/ffmpeg and the local models.
Model loading is timed separately (prewarm) so the reported video path is the warm, per-request cost.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from veriloop.contracts import InputKind, MediaKind, Platform, VerifyRequest
from veriloop.db.engine import Database
from veriloop.services import extraction, ingestion

from .helpers import new_job
from .smoke import Stopwatch, emit, live_deps

CASE_B = "https://www.youtube.com/shorts/XeYAGU1Z6l8"

pytestmark = [pytest.mark.network, pytest.mark.ml, pytest.mark.timeout(900)]  # cold model loads


async def test_case_b_video_path(db: Database) -> None:
    watch = Stopwatch()
    async with live_deps(db) as deps:
        await extraction.prewarm(deps)
        watch.lap("prewarm_models_s")
        job = await new_job(deps.db, url=CASE_B)
        with capture_logs() as logs:
            resolved = await ingestion.resolve_input(VerifyRequest(url=CASE_B), None, job, deps)
            watch.lap("resolve_s")
            result = await extraction.extract_claims(resolved, "en-IN", deps)
            watch.lap("extract_s")

    resolve_log = next(e for e in logs if e.get("event") == "resolve_input_done")
    extract_log = next(e for e in logs if e.get("event") == "extract_claims_done")
    report = {
        "timings_s": {
            **watch.marks,
            "video_path_total_s": round(watch.marks["resolve_s"] + watch.marks["extract_s"], 2),
        },
        "resolve_steps_ms": resolve_log.get("timings_ms"),
        "extract_steps_ms": extract_log.get("timings_ms"),
        "fetch_methods": resolved.fetch_methods,
        "frames": len(resolved.media),
        "sampling": resolved.sampling.model_dump() if resolved.sampling else None,
        "labels": resolved.labels,
        "title": resolved.title,
        "llm_used": result.llm_used,
        "language": result.language,
        "transcript": (result.transcript or "")[:300],
        "transcript_language": result.transcript_language,
        "ocr_text": (result.ocr_text or "")[:300],
        "claims": [c.model_dump(include={"id", "text", "text_en", "claim_type"}) for c in result.claims],
        "check_worthiness": result.check_worthiness,
        "asr_ocr_mismatch": result.asr_ocr_mismatch,
        "notes": result.notes,
    }
    emit("case_b_video_path", report)

    assert resolved.input_kind is InputKind.URL and resolved.platform is Platform.YOUTUBE
    assert "yt-dlp" in resolved.fetch_methods and resolved.video_path and resolved.audio_path
    assert resolved.media and all(a.kind is MediaKind.VIDEO_FRAME and a.phash for a in resolved.media)
    assert resolved.sampling is not None and resolved.sampling.frames == len(resolved.media)
    assert result.claims, "the title/caption carries a checkable claim"
