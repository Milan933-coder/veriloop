"""fuse() and decide(): BUILD_SPEC §7 decision rules. Pure and deterministic (reads only static config).

`fuse` computes the signals, T, p_true = Platt(T), M, the independent group counts and a pre-debate
candidate (rules 1-5). `decide` takes the candidate through the debate checks (rule 6), the G1/G4/D5
final citation gate (rule 7) and report/legal eligibility (rule 8).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from veriloop.contracts import (
    AbstainReason,
    BranchResult,
    BranchStatus,
    ClaimExtraction,
    DebateResult,
    EvidenceItem,
    EvidenceState,
    ForensicResult,
    ForensicStatus,
    FusionResult,
    ProvenanceResult,
    RationaleSentence,
    Stance,
    Verdict,
    VerdictLabel,
)
from veriloop.core.config import Settings
from veriloop.services.fusion import signals as sig
from veriloop.services.fusion.config import Calibration, FusionConfig, load_calibration, load_fusion_config

DECISIVE = frozenset({VerdictLabel.TRUE, VerdictLabel.FALSE, VerdictLabel.MANIPULATED})
# Tools whose joint failure means "we could not look" rather than "there is nothing" (TOOL_OUTAGE).
KEY_TOOLS = frozenset({"gnews_rss", "bing_news_rss", "wire_rss", "claimreview_feed", "factcheck", "serper"})
# Languages the keyless path can search (Google News / Bing locales + multilingual-e5); anything else
# without an English rendering is LANGUAGE_UNSUPPORTED when nothing is found (E9).
SEARCHABLE_LANGUAGES = frozenset(
    "en hi bn te ta mr gu kn ml pa ur or as ne es fr de pt it nl ar fa id ms vi th zh ja ko ru uk pl tr"
    "".split()
)


@dataclass
class _Analysis:
    """Everything decide needs that is derived from the evidence (recomputed for all_items in decide)."""

    tally: sig.GroupTally
    matches: list[sig.ReviewMatch]
    manipulation: sig.Manipulation
    relevant: list[EvidenceItem]


def _analyse(items: Sequence[EvidenceItem], forensic, provenance, cfg: FusionConfig) -> _Analysis:
    relevant = [
        i
        for i in items
        if i.tool not in sig.ENTITY_TOOLS
        and (
            i.confidence_relevance >= cfg.p("relevance_min")
            or i.stance in (Stance.SUPPORT, Stance.REFUTE)
            or (i.claimreview is not None and i.claimreview.similarity >= cfg.p("claimreview_min_similarity"))
        )
    ]
    return _Analysis(
        tally=sig.tally_groups(items, cfg),
        matches=sig.claimreview_matches(items, cfg),
        manipulation=sig.manipulation_axis(items, forensic, provenance, cfg),
        relevant=relevant,
    )


def _thresholds(settings: Settings, cfg: FusionConfig) -> dict[str, float]:
    return {
        "abstain_threshold": settings.abstain_threshold,
        "checkworthiness_threshold": settings.checkworthiness_threshold,
        "manipulation_min": settings.manipulation_min,
        "nli_entail_min": settings.nli_entail_min,
        "forensic_low": settings.forensic_low,
        "forensic_high": settings.forensic_high,
        "forensic_delta": settings.forensic_delta,
        **{k: float(v) for k, v in cfg.params.items()},
    }


def _not_checkable(extraction: ClaimExtraction | None, settings: Settings) -> str | None:
    if extraction is None or not extraction.claims:
        return "No checkable claim was found in the input (E3)."
    if extraction.check_worthiness < settings.checkworthiness_threshold:
        why = extraction.worthiness_reason or "opinion, greeting or non-factual content"
        return (
            f"Check-worthiness {extraction.check_worthiness:.2f} is below "
            f"{settings.checkworthiness_threshold:.2f}: {why}"
        )
    return None


def _tool_outage(evidence: BranchResult | None, degraded_tools: Sequence[str] = ()) -> bool:
    if evidence is None:
        return True
    if evidence.status is BranchStatus.FAILED:
        return True
    failed = {e.tool for e in evidence.errors if e.error_type != "unconfigured"} | set(degraded_tools)
    return len(failed & KEY_TOOLS) >= 2


def _language_unsupported(extraction: ClaimExtraction) -> bool:
    lang = (extraction.language or "en").split("-")[0].lower()
    claim = extraction.primary_claim()
    has_english = lang == "en" or bool(claim and claim.text_en)
    return lang not in SEARCHABLE_LANGUAGES and not has_english


def _evidence_state(a: _Analysis, t: float, m_ok: bool) -> EvidenceState:
    gs, gr = a.tally.groups_support, a.tally.groups_refute
    if not a.relevant:
        return EvidenceState.NO_EVIDENCE
    if m_ok:
        return EvidenceState.MANIPULATED
    if gs >= 2 and gr >= 2 and gs < 2 * gr and gr < 2 * gs:
        return EvidenceState.CONFLICTING
    if gr > gs and t < 0:
        return EvidenceState.REFUTES
    if gs > gr and t > 0:
        return EvidenceState.SUPPORTS
    return EvidenceState.INSUFFICIENT


def _candidate(
    extraction: ClaimExtraction,
    a: _Analysis,
    t: float,
    p_true: float,
    forensic: ForensicResult | None,
    evidence: BranchResult | None,
    settings: Settings,
    cfg: FusionConfig,
    degraded_tools: Sequence[str] = (),
) -> tuple[VerdictLabel, AbstainReason | None, str]:
    """Rules 2-5 (rule 1 is handled by the callers). Returns (label, reason, detail)."""
    thr = settings.abstain_threshold
    gs, gr = a.tally.groups_support, a.tally.groups_refute
    man = a.manipulation
    gm = len(man.groups)
    forensic_manip = forensic is not None and forensic.status is ForensicStatus.MANIPULATED
    # 2. E5: strong textual support but forensics say manipulated -> abstain, cite both sides.
    if t >= cfg.p("conflict_truth_score") and gs >= 2 and forensic_manip:
        return (
            VerdictLabel.UNVERIFIABLE,
            AbstainReason.CONFLICT,
            f"{gs} independent sources support the claim (T={t:+.2f}) but forensic analysis says the "
            "media is "
            f"manipulated (P(fake)={forensic.ensemble_score if forensic else 0:.2f}). Both sides are cited.",
        )
    # 3. MANIPULATED: a manipulation finding AND M >= MANIPULATION_MIN AND >= 2 documenting groups.
    if (man.forensic_manipulated or man.known_fake_manipulated or man.claimreview_manipulated) and (
        man.score >= settings.manipulation_min and gm >= 2
    ):
        return (
            VerdictLabel.MANIPULATED,
            None,
            f"M={man.score:.2f}, {gm} independent groups document manipulation",
        )
    # 4. TRUE / FALSE / CONFLICT
    if p_true >= thr and gs >= 2 and gs >= 2 * gr:
        return VerdictLabel.TRUE, None, f"p_true={p_true:.2f}, {gs} supporting vs {gr} refuting groups"
    if (1 - p_true) >= thr and gr >= 2 and gr >= 2 * gs:
        return VerdictLabel.FALSE, None, f"p_false={1 - p_true:.2f}, {gr} refuting vs {gs} supporting groups"
    if gs >= 2 and gr >= 2:
        return (
            VerdictLabel.UNVERIFIABLE,
            AbstainReason.CONFLICT,
            f"Independent sources disagree: {gs} groups support and {gr} groups refute the claim.",
        )
    # 5. most specific abstain reason
    if not a.relevant:
        if _tool_outage(evidence, degraded_tools):
            failed = sorted(
                {e.tool for e in (evidence.errors if evidence else []) if e.error_type != "unconfigured"}
            )
            return (
                VerdictLabel.UNVERIFIABLE,
                AbstainReason.TOOL_OUTAGE,
                "Key evidence tools failed ("
                + (", ".join(failed) or "evidence branch")
                + "); nothing was retrieved.",
            )
        if _language_unsupported(extraction):
            return (
                VerdictLabel.UNVERIFIABLE,
                AbstainReason.LANGUAGE_UNSUPPORTED,
                f"No evidence found and language '{extraction.language}' could not be searched (E9).",
            )
        return (
            VerdictLabel.UNVERIFIABLE,
            AbstainReason.NO_EVIDENCE,
            "No relevant evidence was found by any tool.",
        )
    leading = max(gs, gr, gm)
    forensic_inconclusive = forensic is not None and forensic.status is ForensicStatus.INCONCLUSIVE
    # SPEC §4: inconclusive forensics + no strong textual evidence (|T| small or < 2 groups) -> abstain.
    if forensic_inconclusive and (abs(t) < cfg.p("strong_text_score") or leading < 2):
        return (
            VerdictLabel.UNVERIFIABLE,
            AbstainReason.FORENSIC_INCONCLUSIVE,
            "Forensic models fall in the abstain band and the textual evidence is not strong "
            f"(T={t:+.2f}, {leading} independent group(s) on the leading side).",
        )
    if leading == 0:
        return (
            VerdictLabel.UNVERIFIABLE,
            AbstainReason.LOW_CONFIDENCE,
            f"{len(a.relevant)} relevant item(s) found, but none supports or refutes the claim "
            "(speculative, hedged or off-target coverage).",
        )
    if leading < 2:
        side = "supporting" if gs >= max(gr, gm) else "refuting" if gr >= gm else "manipulation-documenting"
        return (
            VerdictLabel.UNVERIFIABLE,
            AbstainReason.SINGLE_SOURCE,
            f"Only one independent {side} source group was found; at least two are required (G4).",
        )
    best = max(p_true, 1 - p_true)
    return (
        VerdictLabel.UNVERIFIABLE,
        AbstainReason.LOW_CONFIDENCE,
        f"Calibrated confidence {best:.2f} is below the abstain threshold {thr:.2f} "
        f"(support groups {gs}, refute groups {gr}, M={man.score:.2f}).",
    )


def fuse(
    extraction: ClaimExtraction,
    evidence: BranchResult | None,
    forensic: ForensicResult | None,
    provenance: ProvenanceResult | None,
    settings: Settings,
    *,
    config: FusionConfig | None = None,
    calibration: Calibration | None = None,
) -> FusionResult:
    cfg = config or load_fusion_config()
    cal = calibration or load_calibration()
    items = [*(evidence.items if evidence else []), *(provenance.items if provenance else [])]
    a = _analyse(items, forensic, provenance, cfg)
    signals = [
        sig.claimreview_signal(a.matches, cfg),
        sig.news_stance_signal(a.tally, cfg),
        sig.gdelt_signal(items, cfg),
        sig.wikidata_signal(items, cfg),
        sig.provenance_signal(provenance, cfg),
    ]
    t = sig.truth_score(signals)
    p_true = cal.p_true(t)
    signals.append(sig.forensic_signal(forensic))
    m_ok = a.manipulation.score >= settings.manipulation_min and len(a.manipulation.groups) >= 1
    notes: list[str] = []
    nc = _not_checkable(extraction, settings)
    if nc:
        label, reason, detail = VerdictLabel.NOT_CHECKABLE, None, nc
        state = EvidenceState.NOT_CHECKABLE
    else:
        label, reason, detail = _candidate(extraction, a, t, p_true, forensic, evidence, settings, cfg)
        state = _evidence_state(a, t, m_ok)
        if reason is AbstainReason.CONFLICT:
            state = EvidenceState.CONFLICTING
    notes.append(detail)
    is_fast = sig.fast_path(a.matches, cfg)
    if is_fast:
        notes.append("fast path: >= 2 independent ClaimReviews agree at high similarity")
    return FusionResult(
        truth_score=round(t, 6),
        manipulation_score=a.manipulation.score,
        p_true=round(p_true, 6),
        signals=signals,
        groups_support=a.tally.groups_support,
        groups_refute=a.tally.groups_refute,
        groups_manipulation=len(a.manipulation.groups),
        candidate=label,
        candidate_reason=reason,
        evidence_state=state,
        weights_version=cfg.version,
        calibration_version=cal.version,
        thresholds=_thresholds(settings, cfg),
        fast_path=is_fast,
        notes=notes,
    )


# ---------------------------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------------------------
def _side_items(label: VerdictLabel, a: _Analysis, by_id: dict[str, EvidenceItem]) -> dict[str, list[str]]:
    """group -> evidence ids on the emitted label's side."""
    if label is VerdictLabel.TRUE:
        return {g: [i.id for i in items] for g, items in a.tally.support.items()}
    if label is VerdictLabel.FALSE:
        return {g: [i.id for i in items] for g, items in a.tally.refute.items()}
    if label is VerdictLabel.MANIPULATED:
        return {g: [i for i in ids if i in by_id] for g, ids in a.manipulation.groups.items()}
    return {}


def _best_per_group(
    groups: dict[str, list[str]], by_id: dict[str, EvidenceItem], limit: int = 6
) -> list[str]:
    picked: list[EvidenceItem] = []
    for ids in groups.values():
        cands = [by_id[i] for i in ids if i in by_id]
        if cands:
            picked.append(
                max(cands, key=lambda i: (i.stance_confidence, i.confidence_relevance, -_num(i.id)))
            )
    picked.sort(key=lambda i: (-i.confidence_relevance, sig.id_sort_key(i.id)))
    return [i.id for i in picked[:limit]]


def _num(item_id: str) -> int:
    return sig.id_sort_key(item_id)[1]


def extractive_rationale(
    label: VerdictLabel, a: _Analysis, by_id: dict[str, EvidenceItem], *, limit: int = 3
) -> list[RationaleSentence]:
    """Quoted evidence sentences with ids (LLM disabled / debate unavailable). Quotes are exact substrings."""
    if label in DECISIVE:
        ids = _best_per_group(_side_items(label, a, by_id), by_id, limit)
    else:
        ids = _best_per_group({g: [i.id for i in v] for g, v in a.tally.support.items()}, by_id, 2)
        ids += _best_per_group({g: [i.id for i in v] for g, v in a.tally.refute.items()}, by_id, 2)
        if not ids:
            ids = [i.id for i in sorted(a.relevant, key=lambda i: -i.confidence_relevance)[:2]]
    out: list[RationaleSentence] = []
    for item_id in ids:
        it = by_id[item_id]
        quote = it.stance_quote or it.title or it.snippet[:200]
        out.append(RationaleSentence(text=f'{it.source_name}: "{quote.strip()}"', evidence_ids=[it.id]))
    return out


def extractive_rationale_for(
    label: VerdictLabel,
    items: Sequence[EvidenceItem],
    forensic: ForensicResult | None = None,
    provenance: ProvenanceResult | None = None,
    *,
    config: FusionConfig | None = None,
    limit: int = 3,
) -> list[RationaleSentence]:
    """Public entry for the debate's extractive mode: quoted evidence sentences for `label`."""
    cfg = config or load_fusion_config()
    a = _analyse(items, forensic, provenance, cfg)
    return extractive_rationale(label, a, {i.id: i for i in items}, limit=limit)


def decide(
    extraction: ClaimExtraction | None,
    fusion: FusionResult | None,
    debate: DebateResult | None,
    forensic: ForensicResult | None,
    provenance: ProvenanceResult | None,
    all_items: Sequence[EvidenceItem],
    settings: Settings,
    *,
    llm_enabled: bool,
    degraded_tools: Sequence[str] = (),
    config: FusionConfig | None = None,
) -> Verdict:
    cfg = config or load_fusion_config()
    thr = settings.abstain_threshold
    labels: list[str] = [] if llm_enabled else ["LLM disabled — extractive rationale"]
    if degraded_tools:
        labels.append("Degraded tools: " + ", ".join(sorted(set(degraded_tools))))
    # 1. NOT_CHECKABLE (E3)
    nc = _not_checkable(extraction, settings)
    if nc or extraction is None:
        worth = extraction.check_worthiness if extraction else 0.0
        return Verdict(
            label=VerdictLabel.NOT_CHECKABLE,
            confidence=round(max(0.0, min(1.0, 1.0 - worth)), 6),
            abstain_detail=nc,
            evidence_state=EvidenceState.NOT_CHECKABLE,
            degraded_tools=sorted(set(degraded_tools)),
            labels=labels,
        )
    if fusion is None:  # orchestrator skipped fusion: nothing was weighed
        return Verdict(
            label=VerdictLabel.UNVERIFIABLE,
            confidence=0.0,
            abstain_reason=AbstainReason.TOOL_OUTAGE,
            abstain_detail="Evidence fusion did not run.",
            evidence_state=EvidenceState.INSUFFICIENT,
            degraded_tools=sorted(set(degraded_tools)),
            labels=labels,
        )
    by_id = {i.id: i for i in all_items}
    a = _analyse(all_items, forensic, provenance, cfg)
    label = fusion.candidate
    reason = fusion.candidate_reason
    detail = fusion.notes[0] if fusion.notes else None
    p_true = fusion.p_true
    if label is VerdictLabel.TRUE:
        confidence = p_true
    elif label is VerdictLabel.FALSE:
        confidence = 1 - p_true
    elif label is VerdictLabel.MANIPULATED:
        confidence = fusion.manipulation_score
    else:  # abstain: strength of the best-supported label (shown as such in the UI)
        confidence = max(p_true, 1 - p_true)
    if label is VerdictLabel.NOT_CHECKABLE:  # fuse saw a non-checkable extraction; keep it consistent
        label, reason = VerdictLabel.UNVERIFIABLE, AbstainReason.LOW_CONFIDENCE

    # 6. debate checks
    judge = debate.judge if debate else None
    verifier = debate.verifier if debate else None
    rationale: list[RationaleSentence] = []
    if judge is not None:
        if verifier is not None:
            ok_idx = {c.sentence_index for c in verifier.checks if c.ok}
            rationale = [s for n, s in enumerate(judge.rationale) if n in ok_idx] if verifier.checks else []
        if judge.extractive:
            labels.append("Extractive rationale (quoted evidence)")
    if label in DECISIVE:
        if judge is not None and judge.label is not label:
            reason, detail = (
                AbstainReason.JUDGE_DISAGREEMENT,
                f"The judge ({judge.model_id}) concluded {judge.label.value} but fusion's candidate "
                f"was {label.value}.",
            )
            label = VerdictLabel.UNVERIFIABLE
        elif judge is not None and (verifier is None or not verifier.ok):
            failing = verifier.failing_sentences if verifier else []
            reason = AbstainReason.UNGROUNDED_RATIONALE
            detail = (
                "The verifier rejected rationale sentence(s) not grounded in this job's evidence: "
                + " | ".join(failing or ["verifier did not run"])
            )
            label = VerdictLabel.UNVERIFIABLE
        elif debate is not None and debate.second_judge is not None and debate.second_judge.agrees is False:
            confidence *= cfg.p("second_judge_penalty")
            labels.append(
                f"Second judge ({debate.second_judge.model_id}) disagreed "
                f"({debate.second_judge.label.value if debate.second_judge.label else 'n/a'}): "
                "confidence reduced"
            )
            if confidence < thr:
                reason = AbstainReason.JUDGE_DISAGREEMENT
                detail = (
                    f"Second judge disagreed; confidence {confidence:.2f} after the x"
                    f"{cfg.p('second_judge_penalty'):.1f} penalty is below the threshold {thr:.2f}."
                )
                label = VerdictLabel.UNVERIFIABLE
    if not rationale:
        rationale = extractive_rationale(label, a, by_id)
        if judge is None and llm_enabled and label in DECISIVE:
            labels.append("Debate unavailable — extractive rationale")

    # 7. G1/G4/D5 final gate: >= 2 independent groups among cited ids, every cited id exists.
    cited: list[str] = []
    for s in rationale:
        cited.extend(s.evidence_ids)
    if label in DECISIVE:
        cited.extend(_best_per_group(_side_items(label, a, by_id), by_id))
    elif reason is AbstainReason.CONFLICT:
        cited.extend(_best_per_group({g: [i.id for i in v] for g, v in a.tally.support.items()}, by_id, 3))
        cited.extend(_best_per_group({g: [i.id for i in v] for g, v in a.tally.refute.items()}, by_id, 3))
        cited.extend(a.manipulation.evidence_ids[:3])
    cited = list(dict.fromkeys(cited))
    unknown = [c for c in cited if c not in by_id]
    if label in DECISIVE:
        side = _side_items(label, a, by_id)
        side_ids = {i for ids in side.values() for i in ids}
        groups = {sig.group_of(by_id[c]) for c in cited if c in side_ids and c in by_id}
        if unknown:
            reason = AbstainReason.UNGROUNDED_RATIONALE
            detail = f"Cited evidence id(s) {unknown} do not exist in this job (G1)."
            label = VerdictLabel.UNVERIFIABLE
        elif len(groups) < 2:
            reason = AbstainReason.SINGLE_SOURCE
            detail = (
                f"Only {len(groups)} independent publisher group(s) among the citations; 2 are required (G4)."
            )
            label = VerdictLabel.UNVERIFIABLE
        elif confidence < thr:
            reason, detail = (
                AbstainReason.LOW_CONFIDENCE,
                f"Confidence {confidence:.2f} is below {thr:.2f} (G2).",
            )
            label = VerdictLabel.UNVERIFIABLE
    cited = [c for c in cited if c in by_id]
    independent = len({sig.group_of(by_id[c]) for c in cited if sig.countable(by_id[c])})

    # 8. eligibility
    report_eligible = label in (VerdictLabel.FALSE, VerdictLabel.MANIPULATED)
    legal_eligible, legal_block = _legal(label, extraction, forensic, len(a.manipulation.groups))
    state = fusion.evidence_state
    if reason is AbstainReason.CONFLICT:
        state = EvidenceState.CONFLICTING
    if fusion.fast_path:
        labels.append("Fast path: independent fact-checks agree")
    return Verdict(
        label=label,
        confidence=round(max(0.0, min(1.0, confidence)), 6),
        abstain_reason=reason if label is VerdictLabel.UNVERIFIABLE else None,
        abstain_detail=detail if label is VerdictLabel.UNVERIFIABLE else None,
        evidence_state=state,
        rationale=rationale,
        cited_evidence_ids=cited,
        independent_source_count=independent,
        report_eligible=report_eligible,
        legal_eligible=legal_eligible,
        legal_block_reason=legal_block,
        degraded_tools=sorted(set(degraded_tools)),
        labels=labels,
    )


def _legal(
    label: VerdictLabel,
    extraction: ClaimExtraction,
    forensic: ForensicResult | None,
    groups_manipulation: int,
) -> tuple[bool, str | None]:
    if label is not VerdictLabel.MANIPULATED:
        return False, "Legal drafts are only offered for MANIPULATED verdicts."
    subject = extraction.subject
    if not subject.is_identifiable_individual:
        return False, "The subject is not an identifiable individual."
    if subject.is_private_individual is not False:  # private or unknown -> extra caution (G9)
        forensic_manip = forensic is not None and forensic.status is ForensicStatus.MANIPULATED
        if not (forensic_manip and groups_manipulation >= 2):
            return False, (
                "Subject may be a private individual (G9): a legal draft needs forensic MANIPULATED and "
                ">= 2 independent corroborating sources."
            )
    return True, None
