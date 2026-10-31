"""run_forensics: faces -> open-model ensemble (+ Sightengine when keyed) -> abstain band -> ForensicResult.

Routing (BUILD_SPEC §5 / task spec): frames with a usable face go to the two face-deepfake detectors
(margin crops); frames without faces go to the AI-generated-image detector. Every raw per-frame score is
kept (persisted to `forensic_scores` per asset, summarised in notes); the ensemble and abstain rules live
in `ensemble.py`. Metadata (EXIF / container / C2PA) is a bonus signal reported alongside, never voted.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from PIL import Image

from veriloop.contracts import (
    AbstainBand,
    BranchStatus,
    ClaimExtraction,
    ForensicResult,
    ForensicStatus,
    MediaAsset,
    MediaKind,
    MetadataReport,
    ModelScore,
    ResolvedInput,
    ToolError,
)
from veriloop.core.config import Settings
from veriloop.core.executor import run_cpu
from veriloop.core.hashing import sha256_hex
from veriloop.core.ids import is_job_id
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.ingestion import ffmpeg
from veriloop.services.ingestion.imaging import hamming, load_rgb, sha256_file, to_jpeg_bytes

from . import faces as faces_mod
from . import metadata, quality, remote
from .classifiers import get_classifier
from .ensemble import decide, top_k_mean
from .models_config import (
    LOCAL_MODELS,
    SIGHTENGINE_MODELS,
    ModelSpec,
    full_model_on_face_frames,
    weight_for,
)

log = structlog.get_logger(__name__)

MAX_DETECT_FRAMES = 16
MAX_FACE_FRAMES = 8
MAX_NOFACE_FRAMES = 4
MAX_REMOTE_FRAMES = 3
# "Absence of C2PA Content Credentials is not evidence of manipulation." (single source: the contract)
C2PA_NOTE = MetadataReport().c2pa_note
SPEECH_NOTE = "No synthetic-speech classifier in the ensemble: audio is transcribed, not authenticated."


@dataclass
class Frame:
    asset_id: str
    path: str
    image: Image.Image
    faces: list[faces_mod.FaceCrop] = field(default_factory=list)

    @property
    def usable_faces(self) -> list[faces_mod.FaceCrop]:
        return [f for f in self.faces if not f.tiny]


@dataclass
class Scoring:
    models: list[ModelScore] = field(default_factory=list)
    per_frame: dict[str, dict[str, float]] = field(default_factory=dict)  # model -> asset -> P(fake)
    faces_found: int = 0
    frames_analyzed: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[ToolError] = field(default_factory=list)
    tools_used: dict[str, str] = field(default_factory=dict)


# =============================================================================================
# reusable scoring core (also used by scripts/eval_forensics.py)
# =============================================================================================
def prepare_frames_blocking(items: list[tuple[str, str]], settings: Settings) -> list[Frame]:
    """Decode each (asset_id, path) and detect faces. Undecodable files are skipped."""
    frames: list[Frame] = []
    for asset_id, path in items:
        try:
            img = load_rgb(path)
        except ValueError:
            continue
        frames.append(Frame(asset_id, path, img, faces_mod.detect_faces(img, settings)))
    return frames


def _spread(items: list[Frame], limit: int) -> list[Frame]:
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step + step / 2)] for i in range(limit)]


async def score_frames(frames: list[Frame], deps: Deps, *, include_remote: bool = True) -> Scoring:
    """Run every applicable model on the frames; one ModelScore per model with top-3 aggregation."""
    out = Scoring(faces_found=sum(len(f.faces) for f in frames))
    face_frames = _spread([f for f in frames if f.usable_faces], MAX_FACE_FRAMES)
    plain_frames = _spread([f for f in frames if not f.usable_faces], MAX_NOFACE_FRAMES)
    full_frames = plain_frames + (face_frames if full_model_on_face_frames() else [])
    out.frames_analyzed = len({f.asset_id for f in face_frames + full_frames})
    if any(f.faces and not f.usable_faces for f in frames):
        out.notes.append(f"Some faces were smaller than {faces_mod.MIN_FACE_PX} px and were not scored")

    local = [
        _score_local(spec, face_frames if spec.role == "face" else full_frames, deps, out)
        for spec in LOCAL_MODELS
    ]
    results = await asyncio.gather(*local)
    out.models.extend(r for r in results if r is not None)
    if include_remote and deps.tools.is_configured("sightengine"):
        out.models.extend(await _score_sightengine(face_frames or full_frames, deps, out))
    elif include_remote:
        out.notes.append(
            "Sightengine unconfigured (SIGHTENGINE_API_USER/SECRET missing): local open-model ensemble only"
        )
    return out


async def _score_local(spec: ModelSpec, frames: list[Frame], deps: Deps, out: Scoring) -> ModelScore | None:
    if not frames:
        return ModelScore(
            model_id=spec.model_id,
            model_version=spec.revision,
            provider="local",
            weight=weight_for(spec.model_id),
            error="not applicable: no face crops"
            if spec.role == "face"
            else "not applicable: all frames have faces",
        )
    if not deps.settings.ml_enable:
        return ModelScore(
            model_id=spec.model_id,
            model_version=spec.revision,
            provider="local",
            weight=weight_for(spec.model_id),
            error="local models disabled (ML_ENABLE=false)",
        )
    inputs: list[tuple[str, Image.Image]] = []
    for frame in frames:
        if spec.role == "face":
            inputs.extend((frame.asset_id, crop.image) for crop in frame.usable_faces)
        else:
            inputs.append((frame.asset_id, frame.image))
    started = time.perf_counter()
    provider = "local"
    scores: list[float] | None = None
    if deps.tools.is_configured("hf_inference") and spec.output == "softmax":
        scores = await _hf_inference(spec, inputs, deps, out)
        provider = "hf_inference" if scores is not None else "local"
    if scores is None:
        clf = get_classifier(spec, deps.settings)
        try:
            scores = await run_cpu(clf.predict, [im for _, im in inputs])
        except Exception as exc:
            return ModelScore(
                model_id=spec.model_id,
                model_version=spec.revision,
                provider="local",
                weight=weight_for(spec.model_id),
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
    per_frame: dict[str, float] = {}
    for (asset_id, _), score in zip(inputs, scores, strict=True):
        per_frame[asset_id] = max(score, per_frame.get(asset_id, 0.0))  # most suspicious face per frame
    out.per_frame[spec.model_id] = per_frame
    out.tools_used[spec.model_id] = spec.revision
    return ModelScore(
        model_id=spec.model_id,
        model_version=spec.revision,
        provider=provider,
        raw_score=top_k_mean(list(per_frame.values())),
        weight=weight_for(spec.model_id),
        frames_scored=len(per_frame),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def _hf_inference(
    spec: ModelSpec, inputs: list[tuple[str, Image.Image]], deps: Deps, out: Scoring
) -> list[float] | None:
    """Remote scoring of the same pinned model; any failure falls back to local (returns None)."""
    scores: list[float] = []
    for _, image in inputs:
        payload = await run_cpu(to_jpeg_bytes, image, quality=95)
        res = await remote.hf_inference_classify(
            deps.tools, deps.settings, spec.model_id, spec.revision, payload, sha256_hex(payload)
        )
        score = remote.parse_hf_labels(res.value, spec.fake_labels) if res.ok else None
        if score is None:
            if res.error:
                out.errors.append(res.error)
            out.notes.append(f"hf_inference failed for {spec.model_id}; scored locally instead")
            return None
        scores.append(score)
    return scores


async def _score_sightengine(frames: list[Frame], deps: Deps, out: Scoring) -> list[ModelScore]:
    started = time.perf_counter()
    per_model: dict[str, dict[str, float]] = {"deepfake": {}, "genai": {}}
    for frame in frames[:MAX_REMOTE_FRAMES]:
        payload = await run_cpu(to_jpeg_bytes, frame.image, quality=95)
        sha = await run_cpu(sha256_file, frame.path)
        res = await remote.sightengine_check(deps.tools, deps.settings, payload, sha)
        if not res.ok:
            if res.error:
                out.errors.append(res.error)
            break
        for name, score in remote.parse_sightengine(res.value).items():
            per_model[name][frame.asset_id] = score
    latency = int((time.perf_counter() - started) * 1000)
    scores: list[ModelScore] = []
    for name, model_id in SIGHTENGINE_MODELS.items():
        frame_scores = per_model[name]
        if name == "deepfake" and not any(f.usable_faces for f in frames):
            continue  # the deepfake model is face-specific
        scores.append(
            ModelScore(
                model_id=model_id,
                model_version=deps.tools.tool_versions().get("sightengine", ""),
                provider="sightengine",
                raw_score=top_k_mean(list(frame_scores.values())),
                weight=weight_for(model_id),
                frames_scored=len(frame_scores),
                latency_ms=latency,
                error=None if frame_scores else "no score returned",
            )
        )
        if frame_scores:
            out.per_frame[model_id] = frame_scores
    out.tools_used["sightengine"] = deps.tools.tool_versions().get("sightengine", "")
    return scores


# =============================================================================================
# run_forensics
# =============================================================================================
async def run_forensics(resolved: ResolvedInput, extraction: ClaimExtraction, deps: Deps) -> ForensicResult:
    started = time.perf_counter()
    s = deps.settings
    band = AbstainBand(low=s.forensic_low, high=s.forensic_high, delta=s.forensic_delta)
    try:
        result = await _run(resolved, extraction, deps, band)
    except Exception as exc:  # never raise to the orchestrator
        log.exception("run_forensics_failed", error=f"{type(exc).__name__}: {exc}")
        result = ForensicResult(
            branch_status=BranchStatus.FAILED,
            status=ForensicStatus.UNAVAILABLE,
            band=band,
            metadata=MetadataReport(),
            notes=[C2PA_NOTE],
            errors=[
                ToolError(
                    tool="forensic",
                    op="run",
                    error_type="exception",
                    message=f"{type(exc).__name__}: {exc}"[:500],
                )
            ],
        )
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "run_forensics_done",
        status=result.status.value,
        branch_status=result.branch_status.value,
        ensemble=result.ensemble_score,
        disagreement=result.disagreement,
        faces=result.faces_found,
        frames=result.frames_analyzed,
        models={m.model_id: m.raw_score for m in result.models},
        latency_ms=result.latency_ms,
    )
    return result


def _candidates(resolved: ResolvedInput) -> list[MediaAsset]:
    """Forensic-grade media only: the post's own images/frames, never thumbnails or page captures."""
    assets = [
        a
        for a in resolved.media
        if a.kind in (MediaKind.IMAGE, MediaKind.VIDEO_FRAME) and a.source in ("download", "upload")
    ]
    # Drop near-identical consecutive frames: scoring the same picture twice only inflates top-3.
    distinct: list[MediaAsset] = []
    for asset in assets:
        if distinct and asset.phash and distinct[-1].phash and hamming(asset.phash, distinct[-1].phash) <= 2:
            continue
        distinct.append(asset)
    return distinct[:MAX_DETECT_FRAMES]


def _quality_issue_blocking(resolved: ResolvedInput, assets: list[MediaAsset]) -> str | None:
    if resolved.video_path and any(a.kind is MediaKind.VIDEO_FRAME for a in assets):
        try:
            info = ffmpeg.probe_blocking(resolved.video_path)
        except ffmpeg.MediaToolError:
            return None
        return quality.video_issue(info.bit_rate, info.width, info.height, info.fps)
    issues = [quality.image_issue(a.path, a.width, a.height) for a in assets if a.kind is MediaKind.IMAGE]
    issues = [i for i in issues if i]
    return (
        issues[0] if issues and len(issues) == len([a for a in assets if a.kind is MediaKind.IMAGE]) else None
    )


async def _run(
    resolved: ResolvedInput, extraction: ClaimExtraction, deps: Deps, band: AbstainBand
) -> ForensicResult:
    base = {
        "band": band,
        "sampling": resolved.sampling,
        "asr_ocr_mismatch": extraction.asr_ocr_mismatch,
    }
    if not resolved.media and not resolved.video_path:
        return ForensicResult(
            branch_status=BranchStatus.SKIPPED,
            status=ForensicStatus.UNAVAILABLE,
            metadata=MetadataReport(),
            notes=["No image or video in the input (text-only analysis)", C2PA_NOTE],
            **base,
        )
    metadata_task = asyncio.ensure_future(_metadata(resolved))
    assets = _candidates(resolved)
    if not assets:
        md = await metadata_task
        return ForensicResult(
            branch_status=BranchStatus.INSUFFICIENT,
            status=ForensicStatus.UNAVAILABLE,
            metadata=md,
            notes=[
                "Visual forensics unavailable: only a platform thumbnail or page capture was accessible",
                _c2pa_line(md),
            ],
            **base,
        )
    if deps.settings.ml_enable and not await run_cpu(faces_mod.yunet_ready, deps.settings):
        await faces_mod.ensure_yunet(deps.settings, deps.tools)  # one-time ~230 KB fetch if prewarm missed it
    issue_task = run_cpu(_quality_issue_blocking, resolved, assets)
    frames = await run_cpu(prepare_frames_blocking, [(a.id, a.path) for a in assets], deps.settings)
    scoring = await score_frames(frames, deps)
    quality_issue = await issue_task
    md = await metadata_task
    decision = decide(scoring.models, band, quality_issue=quality_issue)

    notes = list(scoring.notes)
    notes.extend(f"Abstain: {r}" for r in decision.reasons)
    detector = faces_mod.backend(deps.settings)
    if detector == faces_mod.NO_DETECTOR:
        notes.append("Face detection unavailable (YuNet not downloaded): whole-image detector only")
    notes.append(f"Face detector: {detector}; faces found: {scoring.faces_found}")
    notes.extend(_frame_score_notes(scoring))
    notes.append(SPEECH_NOTE)
    notes.append(_c2pa_line(md))
    if extraction.asr_ocr_mismatch is not None and extraction.asr_ocr_mismatch >= 0.6:
        notes.append(f"Spoken words and on-screen text disagree (mismatch {extraction.asr_ocr_mismatch:.2f})")

    model_errors = [m for m in scoring.models if m.error and not m.error.startswith("not applicable")]
    if decision.status is ForensicStatus.UNAVAILABLE:
        branch = BranchStatus.INSUFFICIENT
    elif model_errors or scoring.errors:
        branch = BranchStatus.DEGRADED
    else:
        branch = BranchStatus.OK
    tools_used = dict(scoring.tools_used)
    tools_used["face_detector"] = faces_mod.backend(deps.settings)
    await _persist_scores(deps, assets, scoring)
    return ForensicResult(
        branch_status=branch,
        status=decision.status,
        ensemble_score=decision.ensemble_score,
        disagreement=decision.disagreement,
        models=scoring.models,
        faces_found=scoring.faces_found,
        frames_analyzed=scoring.frames_analyzed,
        metadata=md,
        notes=notes,
        errors=scoring.errors,
        tools_used=tools_used,
        **base,
    )


def _c2pa_line(md: MetadataReport) -> str:
    """User-visible C2PA line; the 'absence is not evidence' sentence is always part of it (SPEC §4)."""
    if md.c2pa_present:
        who = ", ".join(x for x in (md.c2pa_signer, md.c2pa_issuer) if x) or "unknown signer"
        return f"C2PA Content Credentials present (signed by {who}). {C2PA_NOTE}"
    return f"No C2PA Content Credentials found. {C2PA_NOTE}"


async def _metadata(resolved: ResolvedInput) -> MetadataReport:
    try:
        return await run_cpu(metadata.analyze_blocking, resolved)
    except Exception as exc:
        log.warning("metadata_failed", error=f"{type(exc).__name__}: {exc}")
        return MetadataReport()


def _frame_score_notes(scoring: Scoring) -> list[str]:
    notes: list[str] = []
    for model_id, frames in scoring.per_frame.items():
        if not frames:
            continue
        shown = ", ".join(f"{aid}={score:.2f}" for aid, score in list(frames.items())[:16])
        notes.append(f"Per-frame P(fake) {model_id}: {shown}")
    return notes


def _job_id_from(path: str, settings: Settings) -> str | None:
    try:
        rel = Path(path).resolve().relative_to((settings.home / "media").resolve())
    except (ValueError, OSError):
        return None
    job_id = rel.parts[0] if rel.parts else None
    return job_id if job_id and is_job_id(job_id) else None


async def _persist_scores(deps: Deps, assets: list[MediaAsset], scoring: Scoring) -> None:
    """Per-frame rows (asset_id set) + per-model aggregates (asset_id NULL) in forensic_scores."""
    job_id = _job_id_from(assets[0].path, deps.settings) if assets else None
    if job_id is None:
        return
    try:
        await repo.insert_forensic_scores(deps.db, job_id, scoring.models)
        for model in scoring.models:
            for asset_id, score in scoring.per_frame.get(model.model_id, {}).items():
                frame_score = model.model_copy(update={"raw_score": score, "frames_scored": 1})
                await repo.insert_forensic_scores(deps.db, job_id, [frame_score], asset_id=asset_id)
    except Exception as exc:  # e.g. no jobs row when called from a script
        log.info("forensic_scores_not_persisted", job_id=job_id, error=f"{type(exc).__name__}: {exc}"[:200])
