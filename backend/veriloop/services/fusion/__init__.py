"""Fusion service (BUILD_SPEC §5, §7): pure, deterministic weighted aggregation + calibrated decision rules.

    fuse(extraction, evidence, forensic, provenance, settings) -> FusionResult
    decide(extraction, fusion, debate, forensic, provenance, all_items, settings, *,
           llm_enabled, degraded_tools) -> Verdict

No network or database I/O; reads only config/weights.yaml and config/calibration.json (cached).
"""

from veriloop.services.fusion.config import (
    Calibration,
    FusionConfig,
    load_calibration,
    load_fusion_config,
)
from veriloop.services.fusion.rules import decide, extractive_rationale, extractive_rationale_for, fuse

__all__ = [
    "Calibration",
    "FusionConfig",
    "decide",
    "extractive_rationale",
    "extractive_rationale_for",
    "fuse",
    "load_calibration",
    "load_fusion_config",
]
