"""fuse(): signals, T, M, group counting, candidate rules 1-5, evidence_state (G8), fast path."""

from __future__ import annotations

import pytest

from veriloop.contracts import (
    AbstainReason,
    BranchStatus,
    EvidenceState,
    ForensicStatus,
    Stance,
    VerdictLabel,
)
from veriloop.services.fusion import fuse, load_calibration, load_fusion_config
from veriloop.services.fusion.config import Calibration

from .factories import (
    constant_calibration,
    error,
    evidence,
    extraction,
    forensic,
    item,
    provenance,
    review,
)

S, R, N = Stance.SUPPORT, Stance.REFUTE, Stance.NEUTRAL


def run(items=(), *, p=None, ext=None, fx=None, prov=None, errors=None, status=BranchStatus.OK, settings):
    cal = constant_calibration(p) if p is not None else None
    return fuse(
        ext or extraction(),
        evidence(list(items), errors=errors, status=status),
        fx or forensic(),
        prov or provenance(),
        settings,
        calibration=cal,
    )


def sig(result, name):
    return next(s for s in result.signals if s.name == name)


# ---- rule 1: NOT_CHECKABLE -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("worthiness", "claims", "expected"),
    [
        (0.39, True, VerdictLabel.NOT_CHECKABLE),
        (0.0, True, VerdictLabel.NOT_CHECKABLE),
        (0.9, False, VerdictLabel.NOT_CHECKABLE),
        (0.40, True, VerdictLabel.UNVERIFIABLE),  # exactly at the threshold is checkable (no evidence)
    ],
)
def test_rule1_not_checkable(settings, worthiness, claims, expected):
    res = run(ext=extraction(worthiness=worthiness, claims=claims), settings=settings)
    assert res.candidate is expected
    if expected is VerdictLabel.NOT_CHECKABLE:
        assert res.evidence_state is EvidenceState.NOT_CHECKABLE
        assert res.candidate_reason is None


# ---- rule 2: CONFLICT (E5) --------------------------------------------------------------------
def test_rule2_wire_true_but_forensic_manipulated_is_conflict(settings):
    items = [item("E1", "reuters", S), item("E2", "the_hindu_group", S)]
    res = run(items, fx=forensic(ForensicStatus.MANIPULATED, 0.95), settings=settings)
    assert res.truth_score >= 0.5
    assert res.candidate is VerdictLabel.UNVERIFIABLE
    assert res.candidate_reason is AbstainReason.CONFLICT
    assert res.evidence_state is EvidenceState.CONFLICTING


def test_rule2_needs_two_supporting_groups(settings):
    items = [item("E1", "reuters", S), item("E2", "reuters", S)]  # syndicated duplicate: one group
    res = run(items, fx=forensic(ForensicStatus.MANIPULATED, 0.95), settings=settings)
    assert res.candidate_reason is not AbstainReason.CONFLICT


def test_rule2_boundary_truth_score_below_half_is_not_conflict(settings):
    # 2 support + 2 on-topic neutral groups -> news_stance = 2/4 = 0.5 * r(0.9) = 0.45 < 0.5
    items = [item("E1", "a", S), item("E2", "b", S), item("E3", "c", N), item("E4", "d", N)]
    res = run(items, p=0.9, fx=forensic(ForensicStatus.MANIPULATED, 0.95), settings=settings)
    assert res.truth_score == pytest.approx(0.45)
    assert res.candidate_reason is not AbstainReason.CONFLICT


# ---- rule 3: MANIPULATED ------------------------------------------------------------------------
def test_rule3_two_manipulation_claimreviews(settings):
    items = [
        review("E1", "boom", rating_text="Deepfake", manipulation=True),
        review("E2", "factly", rating_text="Altered", manipulation=True),
    ]
    res = run(items, settings=settings)
    assert res.manipulation_score == pytest.approx(0.9)
    assert res.groups_manipulation == 2
    assert res.candidate is VerdictLabel.MANIPULATED
    assert res.evidence_state is EvidenceState.MANIPULATED


def test_rule3_forensics_alone_never_suffices(settings):
    res = run([], fx=forensic(ForensicStatus.MANIPULATED, 0.99), settings=settings)
    assert res.manipulation_score == pytest.approx(0.99)
    assert res.groups_manipulation == 0
    assert res.candidate is VerdictLabel.UNVERIFIABLE


def test_rule3_single_manipulation_group_is_not_enough(settings):
    items = [
        review("E1", "boom", rating_text="Deepfake", manipulation=True),
        review("E2", "boom", rating_text="Deepfake", manipulation=True),  # same group
    ]
    res = run(items, settings=settings)
    assert res.groups_manipulation == 1
    assert res.candidate is not VerdictLabel.MANIPULATED


@pytest.mark.parametrize(
    ("score", "expected"), [(0.80, VerdictLabel.MANIPULATED), (0.79, VerdictLabel.UNVERIFIABLE)]
)
def test_rule3_manipulation_min_boundary(settings, score, expected):
    # news reports of a debunk (refute + manipulation words, no ClaimReview) + forensic MANIPULATED
    items = [
        item("E1", "ht_media", R, title="Army debunks deepfake video of general"),
        item("E2", "times_group", R, title="Deepfake video of general is fake, says Army"),
    ]
    res = run(items, p=0.5, fx=forensic(ForensicStatus.MANIPULATED, score), settings=settings)
    assert res.groups_manipulation == 2
    assert res.manipulation_score == pytest.approx(score)
    assert res.candidate is expected


def test_rule3_known_fake_with_two_source_groups(settings):
    prov = provenance(
        known_fake="manipulated",
        source_urls=["https://www.boomlive.in/fact-check/x", "https://factly.in/y"],
    )
    res = run([], prov=prov, settings=settings)
    assert res.manipulation_score == pytest.approx(0.9)
    assert res.groups_manipulation == 2
    assert res.candidate is VerdictLabel.MANIPULATED


def test_inconclusive_forensic_does_not_raise_m(settings):
    res = run([], fx=forensic(ForensicStatus.INCONCLUSIVE, 0.6), settings=settings)
    assert res.manipulation_score == 0.0


# ---- rule 4: TRUE / FALSE / CONFLICT ------------------------------------------------------------
@pytest.mark.parametrize(
    ("support", "refute", "p", "label", "reason"),
    [
        (2, 0, 0.70, VerdictLabel.TRUE, None),  # boundary: p == threshold
        (2, 0, 0.6999, VerdictLabel.UNVERIFIABLE, AbstainReason.LOW_CONFIDENCE),
        (2, 1, 0.90, VerdictLabel.TRUE, None),  # 2 >= 2*1
        (3, 2, 0.90, VerdictLabel.UNVERIFIABLE, AbstainReason.CONFLICT),  # neither dominates
        (1, 0, 0.99, VerdictLabel.UNVERIFIABLE, AbstainReason.SINGLE_SOURCE),
        (0, 2, 0.30, VerdictLabel.FALSE, None),  # boundary: 1 - p == threshold
        (0, 2, 0.3001, VerdictLabel.UNVERIFIABLE, AbstainReason.LOW_CONFIDENCE),
        (1, 2, 0.10, VerdictLabel.FALSE, None),
        (2, 3, 0.10, VerdictLabel.UNVERIFIABLE, AbstainReason.CONFLICT),
        (2, 2, 0.50, VerdictLabel.UNVERIFIABLE, AbstainReason.CONFLICT),
        (0, 1, 0.01, VerdictLabel.UNVERIFIABLE, AbstainReason.SINGLE_SOURCE),
        (4, 1, 0.95, VerdictLabel.TRUE, None),
    ],
)
def test_rule4_table(settings, support, refute, p, label, reason):
    items = [item(f"E{i + 1}", f"s{i}", S) for i in range(support)]
    items += [item(f"E{support + i + 1}", f"r{i}", R) for i in range(refute)]
    res = run(items, p=p, settings=settings)
    assert (res.groups_support, res.groups_refute) == (support, refute)
    assert res.candidate is label
    assert res.candidate_reason is reason


def test_two_group_rule_collapses_syndicated_duplicates(settings):
    # Two PTI copies (same publisher_group) + an aggregator page: still one independent group.
    items = [
        item("E1", "pti", S, domain="thehindu.com"),
        item("E2", "pti", S, domain="ndtv.com"),
        item("E3", "aggregator:msn", S, domain="msn.com"),
    ]
    res = run(items, p=0.99, settings=settings)
    assert res.groups_support == 1
    assert res.candidate_reason is AbstainReason.SINGLE_SOURCE


def test_group_with_mixed_stances_uses_net_stance(settings):
    items = [
        item("E1", "a", S, conf=0.9),
        item("E2", "a", R, conf=0.4),
        item("E3", "b", S),
        item("E4", "c", R, conf=0.5),
        item("E5", "c", S, conf=0.5),  # tie -> group c is not decisive
    ]
    res = run(items, p=0.95, settings=settings)
    assert (res.groups_support, res.groups_refute) == (2, 0)
    assert res.candidate is VerdictLabel.TRUE


# ---- G8: FALSE vs NO_EVIDENCE ---------------------------------------------------------------------
def test_no_evidence_is_not_refutes(settings):
    res = run([], settings=settings)
    assert res.candidate is VerdictLabel.UNVERIFIABLE
    assert res.candidate_reason is AbstainReason.NO_EVIDENCE
    assert res.evidence_state is EvidenceState.NO_EVIDENCE
    assert res.truth_score == 0.0
    assert all(not s.present for s in res.signals if s.name != "forensic")


def test_refuted_claim_has_refutes_state(settings):
    items = [review("E1", "factly"), review("E2", "boom"), item("E3", "ht_media", R)]
    res = run(items, settings=settings)
    assert res.evidence_state is EvidenceState.REFUTES
    assert res.truth_score < 0
    assert res.candidate is VerdictLabel.FALSE


def test_irrelevant_items_only_is_no_evidence(settings):
    items = [item("E1", "a", N, relevance=0.1), item("E2", "b", Stance.UNKNOWN, relevance=0.29)]
    res = run(items, settings=settings)
    assert res.candidate_reason is AbstainReason.NO_EVIDENCE
    assert res.evidence_state is EvidenceState.NO_EVIDENCE


def test_hedged_neutral_coverage_is_insufficient_low_confidence(settings):
    # Demo case (d): plenty of on-topic but speculative coverage, nothing decisive.
    items = [item(f"E{i}", f"g{i}", N, relevance=0.85) for i in range(1, 7)]
    res = run(items, settings=settings)
    assert res.candidate_reason is AbstainReason.LOW_CONFIDENCE
    assert res.evidence_state is EvidenceState.INSUFFICIENT
    assert sig(res, "news_stance").present and sig(res, "news_stance").score == 0.0


# ---- rule 5: abstain reasons ----------------------------------------------------------------------
def test_tool_outage_when_key_tools_failed_and_nothing_found(settings):
    errs = [error("gnews_rss"), error("bing_news_rss", "circuit_open")]
    res = run([], errors=errs, status=BranchStatus.INSUFFICIENT, settings=settings)
    assert res.candidate_reason is AbstainReason.TOOL_OUTAGE


def test_unconfigured_keyed_tools_are_not_an_outage(settings):
    errs = [error("factcheck", "unconfigured"), error("serper", "unconfigured")]
    res = run([], errors=errs, settings=settings)
    assert res.candidate_reason is AbstainReason.NO_EVIDENCE


def test_failed_branch_is_tool_outage(settings):
    res = run([], status=BranchStatus.FAILED, settings=settings)
    assert res.candidate_reason is AbstainReason.TOOL_OUTAGE


def test_language_unsupported_without_translation(settings):
    res = run([], ext=extraction(language="xh"), settings=settings)
    assert res.candidate_reason is AbstainReason.LANGUAGE_UNSUPPORTED


def test_supported_language_without_evidence_is_no_evidence(settings):
    res = run([], ext=extraction(language="hi", text_en="English rendering"), settings=settings)
    assert res.candidate_reason is AbstainReason.NO_EVIDENCE


def test_forensic_inconclusive_with_weak_text(settings):
    items = [item("E1", "a", S)]
    res = run(items, fx=forensic(ForensicStatus.INCONCLUSIVE, 0.5), settings=settings)
    assert res.candidate_reason is AbstainReason.FORENSIC_INCONCLUSIVE


def test_forensic_inconclusive_does_not_mask_strong_text(settings):
    items = [item("E1", "a", S), item("E2", "b", S)]
    res = run(items, p=0.9, fx=forensic(ForensicStatus.INCONCLUSIVE, 0.5), settings=settings)
    assert res.candidate is VerdictLabel.TRUE


# ---- signals ---------------------------------------------------------------------------------------
def test_absent_signals_are_excluded_from_the_average(settings):
    items = [item("E1", "a", S, conf=1.0), item("E2", "b", S, conf=1.0)]
    res = run(items, settings=settings)
    # only news_stance present: T = 1.0 * 1.0 (not diluted by absent claimreview/gdelt/wikidata/provenance)
    assert res.truth_score == pytest.approx(1.0)
    assert [s.name for s in res.signals if s.present and s.weight > 0] == ["news_stance"]


def test_weighted_average_over_present_signals(settings):
    items = [review("E1", "factly.in", similarity=0.8), item("E2", "b", R, conf=1.0)]
    prov = provenance(out_of_context=0.5)
    res = run(items, prov=prov, settings=settings)
    cfg = load_fusion_config()
    rel_cr = 1.0  # factly is an IFCN verified signatory, published rating
    cr = -1.0 * 0.8 * rel_cr
    news = (0 - 2) / 2  # both groups refute
    prov_s = -0.7 * 0.5
    w = cfg.weights
    expected = (w["claimreview"] * cr + w["news_stance"] * news * 0.95 + w["provenance"] * prov_s) / (
        w["claimreview"] + w["news_stance"] + w["provenance"]
    )
    assert res.truth_score == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize(("similarity", "present"), [(0.60, True), (0.5999, False)])
def test_claimreview_similarity_threshold(settings, similarity, present):
    res = run([review("E1", "factly", similarity=similarity)], settings=settings)
    assert sig(res, "claimreview").present is present


def test_claimreview_neutral_stance_means_different_claim(settings):
    res = run([review("E1", "factly", stance=Stance.NEUTRAL)], settings=settings)
    assert not sig(res, "claimreview").present


def test_claimreview_support_stance_flips_sign_of_negated_review(settings):
    # Review of the NEGATION of our claim rated False -> supports our claim.
    res = run([review("E1", "factly", rating=-1.0, stance=Stance.SUPPORT, similarity=0.9)], settings=settings)
    assert sig(res, "claimreview").score == pytest.approx(0.9)


def test_headline_detected_rating_has_lower_reliability(settings):
    it = review("E1", "boomlive.in", rating_text="Fake (detected in headline)")
    res = run([it], settings=settings)
    assert sig(res, "claimreview").reliability == pytest.approx(0.8)


def test_gdelt_absent_without_coverage_and_saturates(settings):
    res = run([item("E1", "a", N, tool="gdelt", relevance=0.4)], settings=settings)
    assert not sig(res, "gdelt").present
    many = [item(f"E{i}", f"g{i}", N, tool="gdelt", relevance=0.7) for i in range(1, 8)]
    res = run(many, settings=settings)
    assert sig(res, "gdelt").present and sig(res, "gdelt").score == 1.0
    two = [item(f"E{i}", f"g{i}", N, tool="gdelt", relevance=0.7) for i in range(1, 3)]
    assert sig(run(two, settings=settings), "gdelt").score == pytest.approx(0.4)


@pytest.mark.parametrize(("stance", "score", "present"), [(R, -1.0, True), (S, 1.0, True), (N, 0.0, False)])
def test_wikidata_signal(settings, stance, score, present):
    res = run([item("E1", "wikidata", stance, tool="wikidata")], settings=settings)
    s = sig(res, "wikidata")
    assert (s.present, s.score) == (present, score)
    assert res.groups_support == 0 and res.groups_refute == 0  # never a publisher group


@pytest.mark.parametrize(
    ("prov_kwargs", "score", "present"),
    [
        ({"known_fake": "manipulated"}, -1.0, True),
        ({"known_fake": "out_of_context"}, -1.0, True),
        ({"known_fake": "authentic_reference"}, 0.0, False),
        ({"out_of_context": 0.8}, -0.56, True),
        ({}, 0.0, False),
    ],
)
def test_provenance_signal(settings, prov_kwargs, score, present):
    s = sig(run([], prov=provenance(**prov_kwargs), settings=settings), "provenance")
    assert s.present is present
    assert s.score == pytest.approx(score)


# ---- fast path -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("reviews", "expected"),
    [
        ([("factly", -1.0, 0.90), ("boom", -1.0, 0.86)], True),
        ([("factly", -1.0, 0.85), ("boom", -0.6, 0.85)], True),  # boundary similarity, both negative
        ([("factly", -1.0, 0.90), ("boom", -1.0, 0.84)], False),
        ([("factly", -1.0, 0.90), ("factly", -1.0, 0.95)], False),  # one group
        ([("factly", -1.0, 0.90), ("boom", 1.0, 0.90)], False),  # disagree
        ([("factly", 1.0, 0.90), ("boom", 1.0, 0.92)], True),
    ],
)
def test_fast_path_flag(settings, reviews, expected):
    items = [review(f"E{i}", g, rating=r, similarity=sim) for i, (g, r, sim) in enumerate(reviews, 1)]
    assert run(items, settings=settings).fast_path is expected


# ---- bookkeeping ------------------------------------------------------------------------------------
def test_versions_thresholds_and_determinism(settings):
    items = [item("E1", "a", S), item("E2", "b", S), review("E3", "factly", rating=1.0, rating_text="True")]
    r1 = run(items, settings=settings)
    r2 = run(list(reversed(items)), settings=settings)
    assert r1 == r2
    assert r1.weights_version == load_fusion_config().version
    assert r1.calibration_version == load_calibration().version
    assert r1.thresholds["abstain_threshold"] == settings.abstain_threshold
    assert r1.thresholds["claimreview_min_similarity"] == 0.6


def test_platt_calibration_is_applied(settings):
    cal = Calibration(version="t", a=2.0, b=-0.5)
    items = [item("E1", "a", S, conf=1.0), item("E2", "b", S, conf=1.0)]
    res = fuse(extraction(), evidence(items), forensic(), provenance(), settings, calibration=cal)
    assert res.p_true == pytest.approx(cal.p_true(1.0))
    assert res.calibration_version == "t"


def test_provenance_items_are_counted(settings):
    prov = provenance(items=[item("P1", "newschecker.in", R)])
    res = run([item("E1", "factly", R)], prov=prov, p=0.05, settings=settings)
    assert res.groups_refute == 2
    assert res.candidate is VerdictLabel.FALSE


@pytest.mark.parametrize(
    ("stances", "state"),
    [
        ((S,), EvidenceState.INSUFFICIENT),
        ((R,), EvidenceState.INSUFFICIENT),
        ((S, S), EvidenceState.SUPPORTS),
    ],
)
def test_evidence_state_needs_two_groups_like_a_verdict(settings, stances, state):
    items = [item(f"E{n}", f"g{n}", st) for n, st in enumerate(stances, 1)]
    assert run(items, p=0.5, settings=settings).evidence_state is state
