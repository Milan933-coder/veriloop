"""SMOKE (ml): the real local ensemble on one real and one fake face image, plus the case (c) deepfake video.

cd backend && $PY scripts/eval_forensics.py          # once: fetches the labelled images to D:
cd backend && $PY -m pytest tests/services/forensic/test_smoke_models.py -m ml -s
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.services.ingestion.helpers import new_job
from tests.services.ingestion.smoke import emit, live_deps, live_settings
from veriloop.contracts import (
    ClaimExtraction,
    InputKind,
    MediaAsset,
    MediaKind,
    Platform,
    ResolvedInput,
    VerifyRequest,
)
from veriloop.db.engine import Database
from veriloop.services import forensic, ingestion
from veriloop.services.ingestion.imaging import describe_image

pytestmark = [pytest.mark.ml, pytest.mark.timeout(900)]  # cold model loads exceed the 120 s default


def eval_samples() -> dict[str, Path]:
    """First real (IMDB-WIKI) and first face-swap fake (InsightFace) from the eval set on D:."""
    root = live_settings().home / "datasets" / "forensics"
    try:
        data = json.loads((root / "samples.json").read_text(encoding="utf-8"))
    except OSError:
        pytest.skip("run scripts/eval_forensics.py first (labelled images live on D:)")
    picks: dict[str, Path] = {}
    for row in data["samples"]:
        key = {"wiki": "real", "insight": "fake"}.get(row["subset"])
        if key and key not in picks:
            picks[key] = root / row["path"]
    return picks


def as_input(path: Path) -> ResolvedInput:
    info = describe_image(path)
    asset = MediaAsset(
        id="M1", kind=MediaKind.IMAGE, path=str(path), sha256=info.sha256, phash=info.phash,
        width=info.width, height=info.height, source="upload",
    )  # fmt: skip
    return ResolvedInput(
        input_kind=InputKind.UPLOAD, platform=Platform.UPLOAD, media=[asset], media_accessible=True
    )


def summary(result: object, seconds: float) -> dict:
    r = result  # ForensicResult
    return {
        "seconds": round(seconds, 2),
        "status": r.status.value,  # type: ignore[attr-defined]
        "branch_status": r.branch_status.value,  # type: ignore[attr-defined]
        "ensemble": r.ensemble_score,  # type: ignore[attr-defined]
        "disagreement": r.disagreement,  # type: ignore[attr-defined]
        "faces": r.faces_found,  # type: ignore[attr-defined]
        "models": {m.model_id: (m.raw_score, m.weight, m.error) for m in r.models},  # type: ignore[attr-defined]
        "notes": [n for n in r.notes if n.startswith(("Abstain", "Face detector"))],  # type: ignore[attr-defined]
    }


async def test_real_and_fake_images(db: Database) -> None:
    picks = eval_samples()
    async with live_deps(db, llm=False) as deps:
        t = time.perf_counter()
        await forensic.prewarm(deps)
        report: dict = {"prewarm_s": round(time.perf_counter() - t, 1)}
        for label, path in picks.items():
            t = time.perf_counter()
            result = await forensic.run_forensics(as_input(path), ClaimExtraction(), deps)
            report[label] = {"file": path.name, **summary(result, time.perf_counter() - t)}
            assert result.models and any(m.raw_score is not None for m in result.models)
            assert result.metadata is not None and result.metadata.c2pa_note
    emit("forensics_real_vs_fake", report)
    assert {"real", "fake"} <= set(report)


async def test_case_c_deepfake_video(db: Database) -> None:
    video = live_settings().home / "demo" / "case_c" / "demo_case_c.mp4"
    if not video.exists():
        pytest.skip("run scripts/fetch_demo_media.py first")
    async with live_deps(db, llm=False) as deps:
        await forensic.prewarm(deps)
        job = await new_job(deps.db)
        resolved = await ingestion.resolve_input(
            VerifyRequest(upload_id="smoke", text="demo c"), str(video), job, deps
        )
        t = time.perf_counter()
        result = await forensic.run_forensics(resolved, ClaimExtraction(), deps)
        report = summary(result, time.perf_counter() - t)
    emit("forensics_case_c_video", report)
    assert result.frames_analyzed > 0
