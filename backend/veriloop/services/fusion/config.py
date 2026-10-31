"""Fusion weights (config/weights.yaml) and Platt calibration (config/calibration.json).

Both files are versioned; the versions travel in FusionResult into every audit payload. Loading is
cached per path. Missing or broken files fall back to the BUILD_SPEC §7 defaults with a logged
warning (and a version string that says so), so a bad edit never crashes a job.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml

from veriloop.core.config import REPO_ROOT

log = structlog.get_logger(__name__)

WEIGHTS_PATH = REPO_ROOT / "config" / "weights.yaml"
CALIBRATION_PATH = REPO_ROOT / "config" / "calibration.json"

SIGNALS = ("claimreview", "news_stance", "gdelt", "wikidata", "provenance")
DEFAULT_WEIGHTS = {
    "claimreview": 0.35,
    "news_stance": 0.25,
    "gdelt": 0.10,
    "wikidata": 0.10,
    "provenance": 0.20,
}
DEFAULT_RELIABILITY = {
    "claimreview_published": 1.0,
    "claimreview_headline": 0.8,
    "claimreview_unknown_publisher": 0.9,
    "gdelt": 0.5,
    "wikidata_consistent": 0.5,
    "wikidata_contradicted": 1.0,
    "provenance": 1.0,
    "news_stance_floor": 0.5,
}
DEFAULT_PARAMS = {
    "claimreview_min_similarity": 0.60,
    "fast_path_similarity": 0.85,
    "conflict_truth_score": 0.50,
    "relevance_min": 0.30,
    "neutral_relevance_min": 0.50,
    "gdelt_relevance_min": 0.50,
    "gdelt_saturation_groups": 5.0,
    "manipulation_claimreview_score": 0.90,
    "manipulation_known_fake_score": 0.90,
    "second_judge_penalty": 0.70,
    "strong_text_score": 0.50,
}


@dataclass(frozen=True)
class FusionConfig:
    version: str
    weights: dict[str, float]
    reliability: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RELIABILITY))
    params: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PARAMS))

    def p(self, name: str) -> float:
        return float(self.params.get(name, DEFAULT_PARAMS[name]))

    def r(self, name: str) -> float:
        return float(self.reliability.get(name, DEFAULT_RELIABILITY[name]))


@dataclass(frozen=True)
class Calibration:
    version: str
    a: float
    b: float
    method: str = "platt"
    n: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def p_true(self, truth_score: float) -> float:
        """Platt scaling: P(true) = sigmoid(a*T + b)."""
        z = self.a * truth_score + self.b
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        ez = math.exp(z)
        return ez / (1.0 + ez)


def default_config() -> FusionConfig:
    return FusionConfig(version="weights-builtin-default", weights=dict(DEFAULT_WEIGHTS))


@lru_cache(maxsize=8)
def load_fusion_config(path: Path = WEIGHTS_PATH) -> FusionConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        weights = {k: float(v) for k, v in (data.get("weights") or {}).items()}
        unknown = set(weights) - set(SIGNALS)
        if unknown or not weights:
            raise ValueError(f"unknown or missing signal weights: {sorted(unknown) or 'none'}")
        if any(w < 0 for w in weights.values()):
            raise ValueError("weights must be >= 0")
        return FusionConfig(
            version=str(data.get("version") or "weights-unversioned"),
            weights={k: weights.get(k, 0.0) for k in SIGNALS},
            reliability={
                **DEFAULT_RELIABILITY,
                **{k: float(v) for k, v in (data.get("reliability") or {}).items()},
            },
            params={**DEFAULT_PARAMS, **{k: float(v) for k, v in (data.get("params") or {}).items()}},
        )
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        log.warning("fusion_weights_fallback", path=str(path), error=str(exc))
        return default_config()


@lru_cache(maxsize=8)
def load_calibration(path: Path = CALIBRATION_PATH) -> Calibration:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Calibration(
            version=str(data.get("version") or "calibration-unversioned"),
            a=float(data["a"]),
            b=float(data["b"]),
            method=str(data.get("method") or "platt"),
            n=int(data.get("n") or 0),
            raw=data,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("calibration_fallback", path=str(path), error=str(exc))
        return Calibration(version="platt-builtin-prior", a=4.0, b=0.0)
