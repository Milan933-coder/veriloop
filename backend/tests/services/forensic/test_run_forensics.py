"""run_forensics routing, abstain behaviour, metadata/C2PA and keyed adapters (stub models, no downloads)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import respx
from PIL import Image

from tests.services.ingestion.helpers import make_image
from veriloop.contracts import (
    BranchStatus,
    ClaimExtraction,
    ForensicStatus,
    InputKind,
    MediaAsset,
    MediaKind,
    Platform,
    ResolvedInput,
)
from veriloop.deps import Deps
from veriloop.services.forensic import faces, metadata, remote, run_forensics, service
from veriloop.services.forensic.models_config import LOCAL_MODELS
from veriloop.services.ingestion.imaging import describe_image

C2PA_SENTENCE = "Absence of C2PA Content Credentials is not evidence of manipulation."
FACE_MODELS = [s.model_id for s in LOCAL_MODELS if s.role == "face"]
FULL_MODEL = next(s.model_id for s in LOCAL_MODELS if s.role == "full")


@pytest.fixture(autouse=True)
def uniform_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing tests must not depend on the generated weights.json (scripts/eval_forensics.py)."""
    monkeypatch.setattr(service, "weight_for", lambda model_id: 1.0)
    monkeypatch.setattr(service, "full_model_on_face_frames", lambda: False)


class StubClassifier:
    def __init__(self, score: float) -> None:
        self.score = score
        self.seen = 0

    def predict(self, images: list[Image.Image]) -> list[float]:
        self.seen += len(images)
        return [self.score] * len(images)


@pytest.fixture
def ml_deps(deps: Deps, make_settings: Any, monkeypatch: pytest.MonkeyPatch) -> Deps:
    """ML on, but YuNet/HF weights are never fetched: the classifiers are stubs."""
    deps.settings = make_settings(ml_enable=True)
    monkeypatch.setattr(faces, "yunet_ready", lambda settings: True)
    return deps


def stub_models(monkeypatch: pytest.MonkeyPatch, scores: dict[str, float]) -> dict[str, StubClassifier]:
    stubs = {model_id: StubClassifier(score) for model_id, score in scores.items()}
    monkeypatch.setattr(service, "get_classifier", lambda spec, settings: stubs[spec.model_id])
    return stubs


def stub_faces(monkeypatch: pytest.MonkeyPatch, *, found: bool, tiny: bool = False) -> None:
    def detect(img: Image.Image, settings: Any) -> list[faces.FaceCrop]:
        if not found:
            return []
        size = 40 if tiny else 120
        return [faces.FaceCrop(img.crop((10, 10, 10 + size, 10 + size)), (10, 10, size, size), 0.95, tiny)]

    monkeypatch.setattr(faces, "detect_faces", detect)
    monkeypatch.setattr(faces, "backend", lambda settings: "stub")


def image_input(
    tmp_path: Path, *, quality: int = 92, source: str = "upload", kind: MediaKind = MediaKind.IMAGE
) -> ResolvedInput:
    path = make_image(tmp_path / f"img_{quality}_{source}.jpg", size=(480, 360), quality=quality)
    info = describe_image(path)
    asset = MediaAsset(
        id="M1",
        kind=kind,
        path=str(path),
        sha256=info.sha256,
        phash=info.phash,
        width=480,
        height=360,
        source=source,
    )
    return ResolvedInput(
        input_kind=InputKind.UPLOAD, platform=Platform.UPLOAD, media=[asset], media_accessible=True
    )


def assert_c2pa_sentence(result: Any) -> None:
    assert result.metadata is not None and result.metadata.c2pa_note == C2PA_SENTENCE
    assert any(C2PA_SENTENCE in n for n in result.notes)


async def test_text_only_is_skipped_with_c2pa_note(deps: Deps) -> None:
    resolved = ResolvedInput(input_kind=InputKind.TEXT, platform=Platform.TEXT, caption_text="claim")
    out = await run_forensics(resolved, ClaimExtraction(), deps)
    assert out.branch_status is BranchStatus.SKIPPED and out.status is ForensicStatus.UNAVAILABLE
    assert (out.band.low, out.band.high, out.band.delta) == (0.35, 0.70, 0.40)
    assert_c2pa_sentence(out)


async def test_ml_disabled_is_unavailable_not_an_error(deps: Deps, tmp_path: Path) -> None:
    out = await run_forensics(image_input(tmp_path), ClaimExtraction(), deps)
    assert out.status is ForensicStatus.UNAVAILABLE and out.branch_status is BranchStatus.INSUFFICIENT
    assert any(m.error and "ML_ENABLE=false" in m.error for m in out.models)
    assert_c2pa_sentence(out)


async def test_face_frames_go_to_face_models(
    ml_deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs = stub_models(monkeypatch, {FACE_MODELS[0]: 0.92, FACE_MODELS[1]: 0.88, FULL_MODEL: 0.1})
    stub_faces(monkeypatch, found=True)
    out = await run_forensics(image_input(tmp_path), ClaimExtraction(), ml_deps)
    assert out.status is ForensicStatus.MANIPULATED and out.branch_status is BranchStatus.OK
    assert out.faces_found == 1 and out.frames_analyzed == 1
    scored = {m.model_id: m for m in out.models}
    assert (
        scored[FACE_MODELS[0]].raw_score == pytest.approx(0.92) and scored[FACE_MODELS[0]].frames_scored == 1
    )
    assert scored[FACE_MODELS[0]].model_version  # pinned revision recorded
    assert scored[FULL_MODEL].raw_score is None and "not applicable" in (scored[FULL_MODEL].error or "")
    assert stubs[FULL_MODEL].seen == 0
    assert any("Sightengine unconfigured" in n for n in out.notes)
    assert any(n.startswith(f"Per-frame P(fake) {FACE_MODELS[0]}: M1=0.92") for n in out.notes)


async def test_frames_without_faces_go_to_ai_image_detector(
    ml_deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One detector alone can never decide: fewer than 2 models -> inconclusive (abstain band)."""
    stubs = stub_models(monkeypatch, {FACE_MODELS[0]: 0.9, FACE_MODELS[1]: 0.9, FULL_MODEL: 0.97})
    stub_faces(monkeypatch, found=False)
    out = await run_forensics(image_input(tmp_path), ClaimExtraction(), ml_deps)
    assert stubs[FULL_MODEL].seen == 1 and stubs[FACE_MODELS[0]].seen == 0
    assert out.status is ForensicStatus.INCONCLUSIVE and out.ensemble_score == pytest.approx(0.97)
    assert any("need >= 2" in n for n in out.notes)


async def test_tiny_faces_are_not_scored(
    ml_deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_models(monkeypatch, {FACE_MODELS[0]: 0.9, FACE_MODELS[1]: 0.9, FULL_MODEL: 0.2})
    stub_faces(monkeypatch, found=True, tiny=True)
    out = await run_forensics(image_input(tmp_path), ClaimExtraction(), ml_deps)
    assert out.faces_found == 1 and any("smaller than" in n for n in out.notes)
    assert {m.model_id for m in out.models if m.raw_score is not None} == {FULL_MODEL}


async def test_disagreeing_models_abstain(
    ml_deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_models(monkeypatch, {FACE_MODELS[0]: 0.95, FACE_MODELS[1]: 0.1, FULL_MODEL: 0.5})
    stub_faces(monkeypatch, found=True)
    out = await run_forensics(image_input(tmp_path), ClaimExtraction(), ml_deps)
    assert out.status is ForensicStatus.INCONCLUSIVE and out.disagreement == pytest.approx(0.85)


async def test_recompressed_media_abstains_e7(
    ml_deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_models(monkeypatch, {FACE_MODELS[0]: 0.95, FACE_MODELS[1]: 0.93, FULL_MODEL: 0.5})
    stub_faces(monkeypatch, found=True)
    out = await run_forensics(image_input(tmp_path, quality=20), ClaimExtraction(), ml_deps)
    assert out.status is ForensicStatus.INCONCLUSIVE
    assert any("heavily recompressed JPEG" in n for n in out.notes)


async def test_thumbnail_only_is_insufficient(ml_deps: Deps, tmp_path: Path) -> None:
    resolved = image_input(tmp_path, source="thumbnail", kind=MediaKind.THUMBNAIL)
    out = await run_forensics(resolved, ClaimExtraction(), ml_deps)
    assert out.branch_status is BranchStatus.INSUFFICIENT and out.status is ForensicStatus.UNAVAILABLE
    assert_c2pa_sentence(out)


async def test_model_failure_degrades(ml_deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def predict(self, images: list[Image.Image]) -> list[float]:
            raise RuntimeError("weights missing")

    stubs: dict[str, Any] = {
        FACE_MODELS[0]: StubClassifier(0.9),
        FACE_MODELS[1]: Broken(),
        FULL_MODEL: StubClassifier(0.1),
    }
    monkeypatch.setattr(service, "get_classifier", lambda spec, settings: stubs[spec.model_id])
    stub_faces(monkeypatch, found=True)
    out = await run_forensics(image_input(tmp_path), ClaimExtraction(), ml_deps)
    assert out.branch_status is BranchStatus.DEGRADED and out.status is ForensicStatus.INCONCLUSIVE
    assert any(m.error and "weights missing" in m.error for m in out.models)


async def test_never_raises(deps: Deps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(service, "_run", boom)
    out = await run_forensics(image_input(tmp_path), ClaimExtraction(), deps)
    assert out.branch_status is BranchStatus.FAILED and out.errors[0].tool == "forensic"
    assert_c2pa_sentence(out)


def test_metadata_exif_and_c2pa_absent(tmp_path: Path) -> None:
    path = tmp_path / "edited.jpg"
    img = Image.new("RGB", (64, 64), (200, 10, 10))
    exif = Image.Exif()
    exif[0x0131] = "Adobe Photoshop 25.0"
    img.save(path, format="JPEG", quality=70, exif=exif)
    asset = MediaAsset(id="M1", kind=MediaKind.IMAGE, path=str(path), sha256="0" * 64, source="upload")
    report = metadata.analyze_blocking(
        ResolvedInput(input_kind=InputKind.UPLOAD, platform=Platform.UPLOAD, media=[asset])
    )
    assert report.exif_present and report.exif_software == "Adobe Photoshop 25.0"
    assert any("Photoshop" in h for h in report.reencode_hints)
    assert any("JPEG quality ~70" in h for h in report.reencode_hints)
    assert not report.c2pa_present and report.c2pa_note == C2PA_SENTENCE


def test_metadata_stripped_exif_on_upload(tmp_path: Path) -> None:
    path = make_image(tmp_path / "noexif.jpg", quality=92)
    asset = MediaAsset(id="M1", kind=MediaKind.IMAGE, path=str(path), sha256="0" * 64, source="upload")
    report = metadata.analyze_blocking(
        ResolvedInput(input_kind=InputKind.UPLOAD, platform=Platform.UPLOAD, media=[asset])
    )
    assert not report.exif_present and report.exif_stripped_suspected and report.container == "jpeg"


def test_parse_remote_payloads() -> None:
    assert remote.parse_sightengine({"type": {"deepfake": 0.91, "ai_generated": 0.12}}) == {
        "deepfake": 0.91,
        "genai": 0.12,
    }
    assert remote.parse_sightengine({}) == {}
    body = [{"label": "Real", "score": 0.3}, {"label": "Fake", "score": 0.7}]
    assert (
        remote.parse_hf_labels(body, ("fake",)) == 0.7 and remote.parse_hf_labels(body, ("deepfake",)) is None
    )


async def test_sightengine_unconfigured_makes_no_request(deps: Deps) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(remote.SIGHTENGINE_URL)
        res = await remote.sightengine_check(deps.tools, deps.settings, b"x", "ab" * 32)
    assert not res.ok and res.error is not None and res.error.error_type == "unconfigured"
    assert not route.called


async def test_sightengine_configured(make_settings: Any, tool_client: Any) -> None:
    from veriloop.core.tool_client import ToolClient

    settings = make_settings(sightengine_api_user="u", sightengine_api_secret="s3cret")
    tools = ToolClient(settings, cache=tool_client.cache)
    try:
        with respx.mock() as mock:
            route = mock.post(remote.SIGHTENGINE_URL).respond(
                json={"status": "success", "type": {"deepfake": 0.8, "ai_generated": 0.1}}
            )
            res = await remote.sightengine_check(tools, settings, b"jpeg-bytes", "cd" * 32)
        assert res.ok and remote.parse_sightengine(res.value) == {"deepfake": 0.8, "genai": 0.1}
        assert route.called and b"s3cret" in route.calls[0].request.content  # secret only in the request
    finally:
        await tools.aclose()


async def test_hf_inference_unconfigured(deps: Deps) -> None:
    res = await remote.hf_inference_classify(deps.tools, deps.settings, "a/b", "rev", b"x", "ef" * 32)
    assert not res.ok and res.error.error_type == "unconfigured"  # type: ignore[union-attr]


def test_weights_file_drives_weights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from veriloop.services.forensic import models_config

    path = tmp_path / "weights.json"
    path.write_text(
        json.dumps({"weights": {"a/b": 0.25}, "full_model_on_face_frames": True}), encoding="utf-8"
    )
    monkeypatch.setattr(models_config, "WEIGHTS_FILE", path)
    models_config.measured.cache_clear()
    try:
        assert models_config.weight_for("a/b") == 0.25
        assert models_config.weight_for("unmeasured/model") == 1.0
        assert models_config.weight_for("sightengine/genai") == models_config.UNMEASURED_WEIGHT
        assert models_config.full_model_on_face_frames() is True
    finally:
        models_config.measured.cache_clear()
