"""Ensemble vote + explicit abstain band (SPEC §4, BUILD_SPEC §4). Pure functions, no I/O.

* per-model score = mean of its top-3 frame scores (a manipulated segment is rarely every frame);
* ensemble = weight-averaged P(fake) over models that produced a score and carry weight > 0;
* INCONCLUSIVE when fewer than 2 such models, when max-min disagreement > DELTA, when every score sits
  inside [LOW, HIGH], when the ensemble itself falls inside (LOW, HIGH), or when the media quality is too
  poor to trust (E7). Only an ensemble >= HIGH can say MANIPULATED: a low-confidence score never does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veriloop.contracts import AbstainBand, ForensicStatus, ModelScore

TOP_K = 3


def top_k_mean(scores: list[float], k: int = TOP_K) -> float | None:
    clean = sorted((float(s) for s in scores if s is not None), reverse=True)
    if not clean:
        return None
    top = clean[:k]
    return round(sum(top) / len(top), 6)


@dataclass
class Decision:
    status: ForensicStatus
    ensemble_score: float | None
    disagreement: float | None
    voters: int
    reasons: list[str] = field(default_factory=list)


def decide(models: list[ModelScore], band: AbstainBand, *, quality_issue: str | None = None) -> Decision:
    voters = [m for m in models if m.raw_score is not None and m.weight > 0]
    if not voters:
        any_scored = any(m.raw_score is not None for m in models)
        why = (
            "no model with a measured weight produced a score" if any_scored else "no model produced a score"
        )
        return Decision(ForensicStatus.UNAVAILABLE, None, None, 0, [why])
    scores = [float(m.raw_score) for m in voters]  # type: ignore[arg-type]
    total_w = sum(m.weight for m in voters)
    ensemble = round(sum(m.weight * float(m.raw_score) for m in voters) / total_w, 6)  # type: ignore[arg-type]
    disagreement = round(max(scores) - min(scores), 6)
    reasons: list[str] = []
    if len(voters) < 2:
        reasons.append(f"only {len(voters)} model produced a weighted score (need >= 2)")
    if disagreement > band.delta:
        reasons.append(f"models disagree by {disagreement:.2f} > DELTA {band.delta:.2f}")
    if all(band.low <= s <= band.high for s in scores):
        reasons.append(f"all scores inside the abstain band [{band.low:.2f}, {band.high:.2f}]")
    if quality_issue:
        reasons.append(quality_issue)
    if reasons:
        return Decision(ForensicStatus.INCONCLUSIVE, ensemble, disagreement, len(voters), reasons)
    if ensemble >= band.high:
        return Decision(ForensicStatus.MANIPULATED, ensemble, disagreement, len(voters), [])
    if ensemble <= band.low:
        return Decision(ForensicStatus.REAL, ensemble, disagreement, len(voters), [])
    reasons.append(f"ensemble {ensemble:.2f} inside the abstain band ({band.low:.2f}, {band.high:.2f})")
    return Decision(ForensicStatus.INCONCLUSIVE, ensemble, disagreement, len(voters), reasons)
