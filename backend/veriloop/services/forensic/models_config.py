"""Forensic model registry: exact Hugging Face ids + revisions, roles, label semantics, vote weights.

Ids and revisions were verified on 2026-10-31 via https://huggingface.co/api/models/<id> (sha) and
each model card / config.json was read for training data and label names (see docs/media.md).
Weights come from `weights.json`, written by `scripts/eval_forensics.py` from the AUC measured on our
own labelled set (w = clip(2 * (AUC - 0.5), 0, 1)); until that file exists every model weighs 1.0.
No accuracy is claimed anywhere that was not measured (D6).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

WEIGHTS_FILE = Path(__file__).with_name("weights.json")
UNMEASURED_WEIGHT = 0.5  # commercial / remote models we could not evaluate (no key during the build)


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    revision: str  # HF commit sha: pinned so a silent upstream re-upload cannot change our scores
    role: Literal["face", "full"]  # face = runs on face crops; full = runs on whole frames without faces
    output: Literal["softmax", "sigmoid"]
    fake_labels: tuple[str, ...]  # lower-cased id2label names that mean "fake" (softmax models)
    card: str  # training data / caveats from the model card


LOCAL_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        model_id="dima806/deepfake_vs_real_image_detection",
        revision="29e4cf9efc543845610045f6ba7e88e5cf9d9301",
        role="face",
        output="softmax",
        fake_labels=("fake",),
        card=(
            "ViT-base (google/vit-base-patch16-224-in21k) fine-tuned on a Kaggle real-vs-fake face set; "
            "apache-2.0. The card warns the data is ~3 years old (concept drift)."
        ),
    ),
    ModelSpec(
        model_id="prithivMLmods/Deep-Fake-Detector-v2-Model",
        revision="3a99ae26f52c7ac7c3a53103b6cf3a8b617f7093",
        role="face",
        output="softmax",
        fake_labels=("deepfake",),
        card=(
            "ViT-base (google/vit-base-patch16-224-in21k) fine-tuned on a curated Realism/Deepfake image "
            "set (card reports 56k eval images); apache-2.0."
        ),
    ),
    ModelSpec(
        model_id="buildborderless/CommunityForensics-DeepfakeDet-ViT",
        revision="ac6ee457bea904a373065754107451793b56db00",
        role="full",
        output="sigmoid",
        fake_labels=(),
        card=(
            "ViT-Small/384 from 'Community Forensics' (CVPR 2025): 2.7M images from 4,803 generators; "
            "single sigmoid logit = P(AI-generated); MIT. Weights re-uploaded 2026-07-22 (pinned)."
        ),
    ),
)

SIGHTENGINE_MODELS = {"deepfake": "sightengine/deepfake", "genai": "sightengine/genai"}


@lru_cache(maxsize=1)
def measured() -> dict:
    """weights.json content ({} when the evaluation has not been run)."""
    try:
        return json.loads(WEIGHTS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("forensic_weights_unreadable", path=str(WEIGHTS_FILE), error=str(exc))
        return {}


def weight_for(model_id: str) -> float:
    entry = (measured().get("weights") or {}).get(model_id)
    if entry is None:
        return UNMEASURED_WEIGHT if model_id.startswith("sightengine/") else 1.0
    return float(entry)


def full_model_on_face_frames() -> bool:
    """Whether the whole-frame detector also votes on frames that contain faces (decided by the eval)."""
    return bool(measured().get("full_model_on_face_frames", False))


def auc_to_weight(auc: float | None) -> float:
    if auc is None:
        return 0.0
    return round(max(0.0, min(1.0, 2.0 * (auc - 0.5))), 4)
