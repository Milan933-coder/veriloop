"""The explicit abstain band (SPEC §4) and the E7 quality gate: pure functions, exact boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.services.ingestion.helpers import make_image
from veriloop.contracts import AbstainBand, ForensicStatus, ModelScore
from veriloop.services.forensic import quality
from veriloop.services.forensic.ensemble import decide, top_k_mean
from veriloop.services.forensic.models_config import auc_to_weight

BAND = AbstainBand(low=0.35, high=0.70, delta=0.40)


def ms(score: float | None, weight: float = 1.0, model: str = "m") -> ModelScore:
    return ModelScore(model_id=model, provider="local", raw_score=score, weight=weight)


def test_top_k_mean_uses_the_three_most_suspicious_frames() -> None:
    assert top_k_mean([0.1, 0.9, 0.8, 0.2, 0.7]) == pytest.approx(0.8)
    assert top_k_mean([0.4]) == 0.4 and top_k_mean([]) is None


def test_two_confident_models_agree_on_fake() -> None:
    d = decide([ms(0.9, model="a"), ms(0.8, model="b")], BAND)
    assert d.status is ForensicStatus.MANIPULATED and d.ensemble_score == pytest.approx(0.85)
    assert d.disagreement == pytest.approx(0.1) and d.voters == 2


def test_two_confident_models_agree_on_real() -> None:
    assert decide([ms(0.05, model="a"), ms(0.2, model="b")], BAND).status is ForensicStatus.REAL


def test_fewer_than_two_models_is_inconclusive() -> None:
    d = decide([ms(0.99, model="a"), ms(None, model="b")], BAND)
    assert d.status is ForensicStatus.INCONCLUSIVE and d.voters == 1
    assert any("need >= 2" in r for r in d.reasons)


def test_zero_weight_models_do_not_vote() -> None:
    """A model whose measured AUC <= 0.5 has weight 0: it is not a voter, so one confident model abstains."""
    d = decide([ms(0.99, model="a"), ms(0.95, weight=0.0, model="b")], BAND)
    assert d.status is ForensicStatus.INCONCLUSIVE and d.voters == 1


def test_disagreement_above_delta_is_inconclusive() -> None:
    d = decide([ms(0.95, model="a"), ms(0.5, model="b"), ms(0.9, model="c")], BAND)
    assert d.disagreement == pytest.approx(0.45) and d.status is ForensicStatus.INCONCLUSIVE
    assert any("DELTA" in r for r in d.reasons)


def test_disagreement_exactly_delta_is_allowed() -> None:
    # Strictly greater than DELTA abstains; 0.40 itself does not (0.9 vs 0.5 -> ensemble 0.7 = HIGH).
    d = decide([ms(0.9, model="a"), ms(0.5, model="b")], BAND)
    assert d.disagreement == pytest.approx(0.4)
    assert not any("DELTA" in r for r in d.reasons)
    assert d.status is ForensicStatus.MANIPULATED


@pytest.mark.parametrize("scores", [(0.35, 0.70), (0.5, 0.6), (0.36, 0.69, 0.4)])
def test_all_scores_inside_band_is_inconclusive(scores: tuple[float, ...]) -> None:
    d = decide([ms(s, model=f"m{i}") for i, s in enumerate(scores)], BAND)
    assert d.status is ForensicStatus.INCONCLUSIVE
    assert any("inside the abstain band" in r for r in d.reasons)


def test_low_confidence_never_becomes_manipulated() -> None:
    # Not all inside the band and agreeing within DELTA, but the weighted ensemble sits in (LOW, HIGH).
    d = decide([ms(0.72, model="a"), ms(0.34, model="b")], BAND)
    assert d.status is ForensicStatus.INCONCLUSIVE and d.ensemble_score == pytest.approx(0.53)


def test_weights_shift_the_ensemble() -> None:
    d = decide([ms(0.9, weight=1.0, model="a"), ms(0.6, weight=0.2, model="b")], BAND)
    assert d.ensemble_score == pytest.approx((0.9 + 0.12) / 1.2)
    assert d.status is ForensicStatus.MANIPULATED


def test_quality_issue_forces_inconclusive_e7() -> None:
    d = decide([ms(0.95, model="a"), ms(0.9, model="b")], BAND, quality_issue="heavily recompressed JPEG")
    assert d.status is ForensicStatus.INCONCLUSIVE and "heavily recompressed JPEG" in d.reasons


def test_no_scores_is_unavailable() -> None:
    assert decide([], BAND).status is ForensicStatus.UNAVAILABLE
    assert decide([ms(None)], BAND).status is ForensicStatus.UNAVAILABLE


def test_auc_to_weight() -> None:
    assert auc_to_weight(0.5) == 0.0 and auc_to_weight(0.4) == 0.0
    assert auc_to_weight(0.75) == 0.5 and auc_to_weight(1.0) == 1.0 and auc_to_weight(None) == 0.0


def test_jpeg_quality_estimate(tmp_path: Path) -> None:
    high = make_image(tmp_path / "q90.jpg", quality=90)
    low = make_image(tmp_path / "q20.jpg", quality=20)
    png = make_image(tmp_path / "a.png")
    assert abs(quality.jpeg_quality(high) - 90) <= 3  # type: ignore[operator]
    assert abs(quality.jpeg_quality(low) - 20) <= 3  # type: ignore[operator]
    assert quality.jpeg_quality(png) is None


def test_image_quality_gate(tmp_path: Path) -> None:
    ok = make_image(tmp_path / "ok.jpg", size=(400, 300), quality=90)
    crushed = make_image(tmp_path / "bad.jpg", size=(400, 300), quality=25)
    assert quality.image_issue(ok, 400, 300) is None
    assert "heavily recompressed" in (quality.image_issue(crushed, 400, 300) or "")
    assert "too small" in (quality.image_issue(ok, 120, 90) or "")


def test_video_quality_gate() -> None:
    assert quality.video_issue(900_000, 720, 1280, 30.0) is None  # ~0.03 bpp
    assert "heavily compressed" in (quality.video_issue(50_000, 720, 1280, 30.0) or "")
    assert "resolution too low" in (quality.video_issue(None, 200, 150, 25.0) or "")
