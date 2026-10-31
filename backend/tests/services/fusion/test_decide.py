"""decide(): rule 1 shortcut, rule 6 (debate checks), rule 7 (G1/G4/D5 gate), rule 8 (report/legal, G9)."""

from __future__ import annotations

import pytest

from veriloop.contracts import (
    AbstainReason,
    DebateResult,
    EvidenceState,
    ForensicStatus,
    GroundingCheck,
    JudgeOutput,
    RationaleSentence,
    SecondJudge,
    Stance,
    VerdictLabel,
    VerifierOutput,
)
from veriloop.services.fusion import decide, fuse

from .factories import constant_calibration, evidence, extraction, forensic, item, provenance, review

S, R = Stance.SUPPORT, Stance.REFUTE


def true_case():
    return [
        item("E1", "the_hindu_group", S),
        item("E2", "network18_reliance", S),
        item("E3", "govt_india", S),
    ]


def false_case():
    return [review("E1", "factly.in"), review("E2", "boomlive.in"), item("E3", "ht_media", R)]


def manip_case():
    return [
        review("E1", "boomlive.in", rating_text="Deepfake", manipulation=True),
        review("E2", "factly.in", rating_text="Altered", manipulation=True),
    ]


def debate_for(label, sentences, *, ok=True, extractive=False, second=None):
    rationale = [RationaleSentence(text=t, evidence_ids=ids) for t, ids in sentences]
    checks = [
        GroundingCheck(sentence_index=i, text=s.text, evidence_ids=s.evidence_ids, deterministic_ok=ok, ok=ok)
        for i, s in enumerate(rationale)
    ]
    return DebateResult(
        judge=JudgeOutput(
            label=label, rationale=rationale, provider="fake", model_id="fake-1", extractive=extractive
        ),
        verifier=VerifierOutput(
            ok=ok,
            checks=checks,
            failing_sentences=[] if ok else [s.text for s in rationale],
            methods=["deterministic"],
        ),
        second_judge=second,
    )


def run(
    items, debate=None, *, ext=None, fx=None, prov=None, p=None, llm_enabled=True, settings, extra_items=()
):
    ext = ext or extraction()
    fx = fx or forensic()
    prov = prov or provenance()
    cal = constant_calibration(p) if p is not None else None
    fusion = fuse(ext, evidence(items), fx, prov, settings, calibration=cal)
    all_items = [*items, *prov.items, *extra_items]
    return fusion, decide(ext, fusion, debate, fx, prov, all_items, settings, llm_enabled=llm_enabled)


def test_true_with_grounded_debate(settings):
    d = debate_for(VerdictLabel.TRUE, [("The Hindu and Moneycontrol report the hike.", ["E1", "E2"])])
    _, v = run(true_case(), d, p=0.92, settings=settings)
    assert v.label is VerdictLabel.TRUE
    assert v.confidence == pytest.approx(0.92)
    assert v.abstain_reason is None
    assert v.independent_source_count >= 2
    assert {"E1", "E2"} <= set(v.cited_evidence_ids)
    assert not v.report_eligible and not v.legal_eligible
    assert v.evidence_state is EvidenceState.SUPPORTS


def test_false_is_report_eligible_not_legal(settings):
    d = debate_for(VerdictLabel.FALSE, [("Factly and BOOM rate the claim false.", ["E1", "E2"])])
    _, v = run(false_case(), d, settings=settings)
    assert v.label is VerdictLabel.FALSE
    assert v.report_eligible and not v.legal_eligible
    assert v.evidence_state is EvidenceState.REFUTES


def test_rule6_judge_disagreement(settings):
    d = debate_for(VerdictLabel.FALSE, [("x", ["E1"])])
    _, v = run(true_case(), d, p=0.95, settings=settings)
    assert v.label is VerdictLabel.UNVERIFIABLE
    assert v.abstain_reason is AbstainReason.JUDGE_DISAGREEMENT


def test_rule6_verifier_rejects_ungrounded_rationale(settings):
    d = debate_for(VerdictLabel.TRUE, [("Everyone knows this happened.", ["E1"])], ok=False)
    _, v = run(true_case(), d, p=0.95, settings=settings)
    assert v.abstain_reason is AbstainReason.UNGROUNDED_RATIONALE
    assert "Everyone knows this happened." in (v.abstain_detail or "")
    # the ungrounded sentence is never shown as rationale
    assert all(s.text != "Everyone knows this happened." for s in v.rationale)


@pytest.mark.parametrize(
    ("p", "label", "conf"), [(0.99, VerdictLabel.TRUE, 0.693), (0.95, VerdictLabel.UNVERIFIABLE, 0.665)]
)
def test_rule6_second_judge_disagreement_penalty(settings, p, label, conf):
    # threshold 0.69: 0.99 * 0.7 = 0.693 stays TRUE; 0.95 * 0.7 = 0.665 abstains
    second = SecondJudge(provider="fake", model_id="fake-2", label=VerdictLabel.FALSE, agrees=False)
    d = debate_for(VerdictLabel.TRUE, [("Reported by two outlets.", ["E1", "E2"])], second=second)
    s = settings.model_copy(update={"abstain_threshold": 0.69})
    _, v = run(true_case(), d, p=p, settings=s)
    assert v.label is label
    assert v.confidence == pytest.approx(conf, abs=1e-3)
    if label is VerdictLabel.UNVERIFIABLE:
        assert v.abstain_reason is AbstainReason.JUDGE_DISAGREEMENT
    assert any("Second judge" in lbl for lbl in v.labels)


def test_second_judge_agreement_keeps_confidence(settings):
    second = SecondJudge(provider="fake", model_id="fake-2", label=VerdictLabel.TRUE, agrees=True)
    d = debate_for(VerdictLabel.TRUE, [("Reported by two outlets.", ["E1", "E2"])], second=second)
    _, v = run(true_case(), d, p=0.9, settings=settings)
    assert v.label is VerdictLabel.TRUE and v.confidence == pytest.approx(0.9)


def test_rule7_unknown_cited_id_fails_gate(settings):
    d = debate_for(VerdictLabel.TRUE, [("Reported widely.", ["E1", "E99"])])
    _, v = run(true_case(), d, p=0.95, settings=settings)
    assert v.label is VerdictLabel.UNVERIFIABLE
    assert v.abstain_reason is AbstainReason.UNGROUNDED_RATIONALE
    assert "E99" not in v.cited_evidence_ids


def test_rule7_followup_items_are_valid_citations(settings):
    d_item = item("D1", "the_wire", Stance.UNKNOWN, tool="gnews_rss")
    d = debate_for(VerdictLabel.TRUE, [("Also reported.", ["E1", "D1"])])
    _, v = run(true_case(), d, p=0.95, settings=settings, extra_items=[d_item])
    assert v.label is VerdictLabel.TRUE
    assert "D1" in v.cited_evidence_ids


def test_no_debate_uses_extractive_rationale_with_exact_quotes(settings):
    items = true_case()
    _, v = run(items, None, p=0.95, llm_enabled=False, settings=settings)
    assert v.label is VerdictLabel.TRUE
    assert "LLM disabled — extractive rationale" in v.labels
    by_id = {i.id: i for i in items}
    for s in v.rationale:
        (eid,) = s.evidence_ids
        quote = s.text.split(': "', 1)[1].rstrip('"')
        assert quote in by_id[eid].snippet or quote == by_id[eid].title


def test_manipulated_public_figure_is_legal_eligible(settings):
    ext = extraction(identifiable=True, private=False, name="Richard Blumenthal")
    d = debate_for(VerdictLabel.MANIPULATED, [("BOOM and Factly call it a deepfake.", ["E1", "E2"])])
    _, v = run(manip_case(), d, ext=ext, settings=settings)
    assert v.label is VerdictLabel.MANIPULATED
    assert v.confidence == pytest.approx(0.9)
    assert v.report_eligible and v.legal_eligible and v.legal_block_reason is None


@pytest.mark.parametrize("private", [True, None])
def test_g9_private_or_unknown_subject_needs_forensic_manipulated(settings, private):
    ext = extraction(identifiable=True, private=private)
    d = debate_for(VerdictLabel.MANIPULATED, [("Two fact-checkers call it a deepfake.", ["E1", "E2"])])
    _, v = run(manip_case(), d, ext=ext, settings=settings)
    assert v.label is VerdictLabel.MANIPULATED
    assert not v.legal_eligible and "G9" in (v.legal_block_reason or "")
    _, v2 = run(manip_case(), d, ext=ext, fx=forensic(ForensicStatus.MANIPULATED, 0.9), settings=settings)
    assert v2.legal_eligible


def test_manipulated_without_identifiable_subject_not_legal(settings):
    d = debate_for(VerdictLabel.MANIPULATED, [("Two fact-checkers call it a deepfake.", ["E1", "E2"])])
    _, v = run(manip_case(), d, settings=settings)
    assert v.report_eligible and not v.legal_eligible


def test_not_checkable_shortcut_without_fusion(settings):
    ext = extraction(worthiness=0.1)
    v = decide(ext, None, None, None, None, [], settings, llm_enabled=True, degraded_tools=[])
    assert v.label is VerdictLabel.NOT_CHECKABLE
    assert v.evidence_state is EvidenceState.NOT_CHECKABLE
    assert v.confidence == pytest.approx(0.9)
    assert not v.report_eligible


def test_conflict_cites_both_sides(settings):
    items = [item("E1", "a", S), item("E2", "b", S), item("E3", "c", R), item("E4", "d", R)]
    _, v = run(items, None, p=0.5, settings=settings)
    assert v.abstain_reason is AbstainReason.CONFLICT
    assert v.evidence_state is EvidenceState.CONFLICTING
    assert {"E1", "E2", "E3", "E4"} <= set(v.cited_evidence_ids)


def test_e5_conflict_cites_both_sides(settings):
    items = [item("E1", "reuters", S), item("E2", "the_hindu_group", S)]
    _, v = run(items, None, fx=forensic(ForensicStatus.MANIPULATED, 0.95), settings=settings)
    assert v.label is VerdictLabel.UNVERIFIABLE and v.abstain_reason is AbstainReason.CONFLICT
    assert {"E1", "E2"} <= set(v.cited_evidence_ids)


def test_abstain_confidence_is_best_supported_label(settings):
    _, v = run([item("E1", "a", R)], None, p=0.2, settings=settings)
    assert v.abstain_reason is AbstainReason.SINGLE_SOURCE
    assert v.confidence == pytest.approx(0.8)


def test_degraded_tools_are_carried(settings):
    ext = extraction()
    fusion = fuse(ext, evidence([]), forensic(), provenance(), settings)
    v = decide(
        ext, fusion, None, forensic(), provenance(), [], settings, llm_enabled=True, degraded_tools=["gdelt"]
    )
    assert v.degraded_tools == ["gdelt"]
    assert any("gdelt" in lbl for lbl in v.labels)
    assert v.evidence_state is EvidenceState.NO_EVIDENCE


def test_extractive_judge_must_still_pass_verifier(settings):
    d = debate_for(VerdictLabel.TRUE, [('The Hindu: "x"', ["E1"])], ok=False, extractive=True)
    _, v = run(true_case(), d, p=0.95, llm_enabled=False, settings=settings)
    assert v.abstain_reason is AbstainReason.UNGROUNDED_RATIONALE
