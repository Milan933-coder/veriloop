"""run_debate: Tool-MAD flow, fast path, extractive mode, dual-key verifier (D-13), D-16 guards."""

from __future__ import annotations

import pytest

from veriloop.contracts import (
    AbstainReason,
    EvidenceItem,
    JudgeOutput,
    RationaleSentence,
    RetrievalMethod,
    Stance,
    VerdictLabel,
)
from veriloop.core.errors import ToolCallFailed
from veriloop.core.llm import DisabledLLM
from veriloop.core.ml import NLIScore
from veriloop.core.testing import FakeLLM, FakeNLI
from veriloop.services.debate import run_debate, service, verify
from veriloop.services.debate.table import inline_ids, normalize_id, split_roles
from veriloop.services.debate.verifier import hypothesis_text
from veriloop.services.fusion import decide, fuse

from ..fusion.factories import constant_calibration, evidence, extraction, forensic, item, provenance, review

S, R = Stance.SUPPORT, Stance.REFUTE


def true_items() -> list[EvidenceItem]:
    return [
        item(
            "E1",
            "govt_india",
            S,
            domain="pib.gov.in",
            title="Cabinet approves 3% hike in Dearness Allowance",
            snippet="Cabinet approves 3% hike in Dearness Allowance for central government employees.",
            quote="Cabinet approves 3% hike in Dearness Allowance",
        ),
        item(
            "E2",
            "the_hindu_group",
            S,
            domain="thehindu.com",
            title="Cabinet approves 3% DA hike",
            snippet="The Union Cabinet approved a 3% hike in dearness allowance on Tuesday.",
            quote="The Union Cabinet approved a 3% hike in dearness allowance",
        ),
        item(
            "E3",
            "network18",
            S,
            domain="moneycontrol.com",
            title="DA rises to 63% of basic pay",
            snippet="Cabinet approves 3% DA hike; allowance rises to 63% of basic pay.",
            quote="Cabinet approves 3% DA hike",
        ),
        review("E4", "factly.in", rating=1.0, rating_text="True", similarity=0.7),
    ]


def setup(settings, items=None, *, p=0.9, fast=False):
    items = items if items is not None else true_items()
    ext = extraction()
    fx, prov = forensic(), provenance()
    fusion = fuse(ext, evidence(items), fx, prov, settings, calibration=constant_calibration(p))
    if fast:
        fusion = fusion.model_copy(update={"fast_path": True})
    return ext, fusion, items, fx, prov


def entail(value: float) -> FakeNLI:
    return FakeNLI(lambda _p, _h: NLIScore(entailment=value, neutral=1 - value, contradiction=0.0))


JUDGE_TRUE = {
    "label": "TRUE",
    "rationale": [
        {"text": "The Union Cabinet approved a 3% hike in dearness allowance.", "evidence_ids": ["E2", "E1"]},
        {"text": "Moneycontrol reports the allowance rises to 63% of basic pay.", "evidence_ids": ["[E3]"]},
    ],
}
ALL_SUPPORTED = {
    "checks": [
        {"index": 0, "supported": True, "reason": "ok"},
        {"index": 1, "supported": True, "reason": "ok"},
    ]
}


def script_debate(llm: FakeLLM, judge=None, verify_out=None, *, pro_query=None, con_query=None) -> None:
    llm.script(
        "proponent",
        {
            "points": [
                {"text": "PIB says the Cabinet approved a 3% DA hike.", "evidence_ids": ["E1"]},
                {"text": "Point citing nothing valid.", "evidence_ids": ["E42"]},
            ],
            "followup_query": pro_query,
        },
    )
    llm.script(
        "opponent",
        {"points": [{"text": "Factly rates it True.", "evidence_ids": ["E4"]}], "followup_query": con_query},
    )
    llm.script("judge", judge or JUDGE_TRUE)
    llm.script("verify", verify_out or ALL_SUPPORTED)


@pytest.fixture
def no_followup(monkeypatch):
    calls: list[str] = []

    async def fake(query, deps, *, start_index=1):
        calls.append(query)
        return []

    monkeypatch.setattr(service, "followup_search", fake)
    return calls


# ---- full debate ------------------------------------------------------------------------------
async def test_full_debate_true_grounded(deps, fake_llm, settings, no_followup):
    deps.nli = entail(0.9)
    deps.llm2 = FakeLLM({"second_judge": JUDGE_TRUE}, model_id="fake-2", tool="llm_secondary")
    script_debate(fake_llm)
    ext, fusion, items, fx, prov = setup(settings)
    assert fusion.candidate is VerdictLabel.TRUE

    result = await run_debate(ext, fusion, items, fx, prov, deps)

    assert not result.skipped
    assert [p.evidence_ids for p in result.proponent.points] == [["E1"]]  # unknown id point dropped
    assert result.judge.label is VerdictLabel.TRUE and not result.judge.extractive
    assert result.judge.rationale[1].evidence_ids == ["E3"]  # "[E3]" normalised
    assert result.verifier.ok and result.verifier.methods == ["deterministic", "llm", "nli"]
    assert all(c.llm_supported and c.nli_entailment == 0.9 for c in result.verifier.checks)
    assert result.second_judge.agrees is True and result.second_judge.model_id == "fake-2"
    efforts = {c.task: c.effort for c in fake_llm.calls}
    assert efforts == {"proponent": "none", "opponent": "none", "judge": "low", "verify": "low"}
    assert no_followup == []

    verdict = decide(ext, fusion, result, fx, prov, items, settings, llm_enabled=True)
    assert verdict.label is VerdictLabel.TRUE
    assert [s.text for s in verdict.rationale] == [s.text for s in result.judge.rationale]


async def test_agents_see_only_their_role_items(deps, fake_llm, settings, no_followup):
    deps.nli = entail(0.9)
    script_debate(fake_llm)
    ext, fusion, items, fx, prov = setup(settings)
    await run_debate(ext, fusion, items, fx, prov, deps)
    pro = fake_llm.calls_for("proponent")[0].user
    con = fake_llm.calls_for("opponent")[0].user
    assert "[E1]" in pro and "[E4]" not in pro
    assert "[E4]" in con and "[E1]" not in con
    judge = fake_llm.calls_for("judge")[0]
    assert all(f"[E{n}]" in judge.user for n in range(1, 5))
    for call in fake_llm.calls:
        if call.task in ("proponent", "opponent", "judge"):
            assert (
                "Never use your own knowledge" in call.system or "never use your own knowledge" in call.system
            )


def test_split_roles():
    items = true_items()
    pro, con = split_roles(items)
    assert [i.id for i in pro] == ["E1", "E2", "E3"]
    assert [i.id for i in con] == ["E4"]


# ---- follow-up searches (ONE per agent, ids D1..Dn) ---------------------------------------------
def _d(n: int, url: str) -> EvidenceItem:
    return EvidenceItem(
        id=f"D{n}",
        source_name="NDTV",
        url=url,
        domain="ndtv.com",
        publisher_group="ndtv",
        snippet="DA hike approved",
        retrieval_method=RetrievalMethod.FOLLOWUP_SEARCH,
        tool="gnews_rss",
        branch="debate",
    )


async def test_followups_are_renumbered_and_deduped(deps, fake_llm, settings, monkeypatch):
    deps.nli = entail(0.9)
    calls = []

    async def fake(query, deps, *, start_index=1):
        calls.append(query)
        if query == "pro q":
            return [_d(1, "https://ndtv.com/a"), _d(2, "https://thehindu.com/article/e2")]
        return [_d(1, "https://ndtv.com/a"), _d(2, "https://boomlive.in/x")]

    monkeypatch.setattr(service, "followup_search", fake)
    items = true_items()
    items[1] = items[1].model_copy(update={"url": "https://thehindu.com/article/e2"})
    script_debate(fake_llm, pro_query="pro q", con_query="con q")
    ext, fusion, items, fx, prov = setup(settings, items)
    fusion = fusion.model_copy(update={"groups_support": 1, "groups_refute": 0})  # both sides thin
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert sorted(calls) == ["con q", "pro q"]
    assert [(i.id, i.url) for i in result.followup_items] == [
        ("D1", "https://ndtv.com/a"),
        ("D2", "https://boomlive.in/x"),
    ]
    assert result.proponent.followup_queries == ["pro q"] and result.proponent.followup_evidence_ids == ["D1"]
    assert result.opponent.followup_evidence_ids == ["D2"]
    assert "[D2]" in fake_llm.calls_for("judge")[0].user


# ---- fast path / extractive / failures ---------------------------------------------------------------
async def test_fast_path_skips_proponent_and_opponent(deps, fake_llm, settings, no_followup):
    deps.nli = entail(0.9)
    script_debate(fake_llm)
    ext, fusion, items, fx, prov = setup(settings, fast=True)
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert result.proponent is None and result.opponent is None
    assert not fake_llm.calls_for("proponent") and not fake_llm.calls_for("opponent")
    assert result.judge.label is VerdictLabel.TRUE and result.verifier.ok
    assert "fast path" in (result.skip_reason or "")


async def test_llm_disabled_is_extractive(deps, settings):
    deps.llm = DisabledLLM()
    deps.nli = entail(0.95)
    ext, fusion, items, fx, prov = setup(settings)
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert result.skipped and "extractive" in result.skip_reason
    judge = result.judge
    assert judge.extractive and judge.label is VerdictLabel.TRUE and judge.provider == "none"
    assert result.verifier.ok and result.verifier.methods == ["deterministic", "exact_quote", "nli"]
    by_id = {i.id: i for i in items}
    for s in judge.rationale:  # every sentence is a verbatim quote of its cited item
        quote = s.text.split(': "', 1)[1].rstrip('"')
        assert any(quote in (by_id[i].title or "") + by_id[i].snippet for i in s.evidence_ids)
    verdict = decide(ext, fusion, result, fx, prov, items, settings, llm_enabled=False)
    assert verdict.label is VerdictLabel.TRUE
    assert "LLM disabled — extractive rationale" in verdict.labels


async def test_judge_failure_falls_back_to_extractive(deps, fake_llm, settings, no_followup):
    deps.nli = entail(0.9)
    script_debate(fake_llm)
    fake_llm.script("judge", ToolCallFailed(_err("llm_primary")))
    ext, fusion, items, fx, prov = setup(settings)
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert result.judge.extractive and result.judge.provider == "extractive-fallback"
    assert result.verifier.ok
    assert not fake_llm.calls_for("verify")  # extractive sentences use the exact-quote key


async def test_agent_failure_does_not_stop_the_judge(deps, fake_llm, settings, no_followup):
    deps.nli = entail(0.9)
    script_debate(fake_llm)
    fake_llm.script("opponent", ToolCallFailed(_err("llm_primary")))
    ext, fusion, items, fx, prov = setup(settings)
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert result.opponent is None and result.proponent is not None
    assert "OPPONENT: (no argument)" in fake_llm.calls_for("judge")[0].user
    assert result.verifier.ok


async def test_run_debate_never_raises(deps, settings, monkeypatch):
    deps.nli = entail(0.9)

    def boom(_items):
        raise RuntimeError("bug")

    monkeypatch.setattr(service, "split_roles", boom)
    ext, fusion, items, fx, prov = setup(settings)
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert result.skipped and "RuntimeError" in result.skip_reason
    assert result.judge.extractive


async def test_no_claim_skips(deps, settings):
    _ext, fusion, items, fx, prov = setup(settings)
    result = await run_debate(extraction(claims=False), fusion, items, fx, prov, deps)
    assert result.skipped and result.judge is None


# ---- second judge -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("script", "agrees", "error"),
    [({"label": "UNVERIFIABLE", "rationale": []}, False, False), (None, None, True)],
)
async def test_second_judge_disagreement_and_error(
    deps, fake_llm, settings, no_followup, script, agrees, error
):
    deps.nli = entail(0.9)
    deps.llm2 = FakeLLM({"second_judge": script} if script else {}, model_id="fake-2", tool="llm_secondary")
    script_debate(fake_llm)
    ext, fusion, items, fx, prov = setup(settings)
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert result.second_judge.agrees is agrees
    assert (result.second_judge.error is not None) is error
    verdict = decide(ext, fusion, result, fx, prov, items, settings, llm_enabled=True)
    if agrees is False:  # 0.9 * 0.7 = 0.63 < 0.70 -> abstain
        assert verdict.label is VerdictLabel.UNVERIFIABLE
        assert verdict.abstain_reason is AbstainReason.JUDGE_DISAGREEMENT
    else:
        assert verdict.label is VerdictLabel.TRUE


async def test_second_judge_disabled_by_setting(deps, fake_llm, make_settings, no_followup):
    deps.settings = make_settings(llm_second_judge=False)
    deps.nli = entail(0.9)
    deps.llm2 = FakeLLM({"second_judge": JUDGE_TRUE}, model_id="fake-2")
    script_debate(fake_llm)
    ext, fusion, items, fx, prov = setup(deps.settings)
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert result.second_judge is None and not deps.llm2.calls


# ---- judge vs fusion -----------------------------------------------------------------------------
async def test_judge_disagreeing_with_fusion_abstains(deps, fake_llm, settings, no_followup):
    deps.nli = entail(0.9)
    script_debate(
        fake_llm,
        judge={
            "label": "UNVERIFIABLE",
            "rationale": [{"text": "PIB reports a 3% DA hike.", "evidence_ids": ["E1"]}],
        },
        verify_out={"checks": [{"index": 0, "supported": True, "reason": "ok"}]},
    )
    ext, fusion, items, fx, prov = setup(settings)
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    verdict = decide(ext, fusion, result, fx, prov, items, settings, llm_enabled=True)
    assert verdict.label is VerdictLabel.UNVERIFIABLE
    assert verdict.abstain_reason is AbstainReason.JUDGE_DISAGREEMENT


# ---- dual-key verifier (D-13) ------------------------------------------------------------------------
def _judge(*sentences, extractive=False) -> JudgeOutput:
    return JudgeOutput(
        label=VerdictLabel.TRUE,
        rationale=[RationaleSentence(text=t, evidence_ids=ids) for t, ids in sentences],
        provider="fake",
        model_id="fake",
        extractive=extractive,
    )


def _err(tool: str):
    from veriloop.contracts import ToolError

    return ToolError(tool=tool, op="chat", error_type="timeout", message="boom")


@pytest.mark.parametrize(
    ("text", "ids", "reason"),
    [
        ("The Cabinet approved a 3% hike.", [], "no evidence ids"),
        ("The Cabinet approved a 3% hike.", ["E99"], "not in this job"),
        ("The Cabinet approved a 3% hike [E7].", ["E1"], "written in the text"),
        ("See https://invented.example/story for the hike.", ["E1"], "URL(s) not returned"),
        ("   ", ["E1"], "empty"),
    ],
)
async def test_verifier_deterministic_failures(deps, fake_llm, text, ids, reason):
    deps.nli = entail(0.99)
    fake_llm.script("verify", {"checks": []})
    out = await verify(_judge((text, ids)), true_items(), deps)
    assert not out.ok
    assert not out.checks[0].deterministic_ok and reason in out.checks[0].reason
    assert out.failing_sentences == [text]


async def test_verifier_accepts_job_urls(deps, fake_llm):
    deps.nli = entail(0.9)
    items = true_items()
    fake_llm.script("verify", {"checks": [{"index": 0, "supported": True, "reason": ""}]})
    out = await verify(_judge((f"PIB published it at {items[0].url}.", ["E1"])), items, deps)
    assert out.ok


async def test_verifier_llm_key_rejects_unsupported(deps, fake_llm):
    deps.nli = entail(0.99)
    fake_llm.script(
        "verify",
        {
            "checks": [
                {"index": 0, "supported": True, "reason": "ok"},
                {"index": 1, "supported": False, "reason": "date not in text"},
            ]
        },
    )
    judge = _judge(("A 3% DA hike was approved.", ["E1"]), ("It was approved on 28 October.", ["E2"]))
    out = await verify(judge, true_items(), deps)
    assert not out.ok
    assert [c.ok for c in out.checks] == [True, False]
    assert out.checks[1].llm_supported is False and "date not in text" in out.checks[1].reason
    assert out.failing_sentences == ["It was approved on 28 October."]


def nli(ent: float, con: float = 0.0) -> FakeNLI:
    return FakeNLI(
        lambda _p, _h: NLIScore(entailment=ent, neutral=max(0.0, 1 - ent - con), contradiction=con)
    )


@pytest.mark.parametrize(
    ("ent", "con", "sentence", "ok", "why"),
    [
        (0.96, 0.0, "A 3% DA hike was approved.", True, ""),
        # low entailment but no contradiction and the cited text covers every word and number -> pass
        (0.20, 0.05, "A 3% DA hike was approved.", True, "lexical coverage"),
        # contradiction veto (changed number / actor): fails even though the words are covered
        (0.00, 0.99, "A 3% DA hike was approved.", False, "contradicts"),
        # a number the cited text does not contain
        (0.00, 0.01, "A 5% DA hike was approved.", False, "number(s) ['5']"),
        # an added fact: NLI says neutral, coverage catches it
        (0.00, 0.01, "Cabinet approved 3% DA and doubled the house rent allowance subsidy.", False, "words"),
    ],
)
async def test_verifier_local_key(deps, fake_llm, ent, con, sentence, ok, why):
    deps.nli = nli(ent, con)
    fake_llm.script("verify", {"checks": [{"index": 0, "supported": True, "reason": ""}]})
    out = await verify(_judge((sentence, ["E1"])), true_items(), deps)
    assert out.ok is ok, out.checks[0].reason
    assert why in out.checks[0].reason
    assert out.checks[0].llm_supported is True and out.checks[0].nli_entailment == ent


async def test_verifier_without_nli_uses_llm_key_only(deps, fake_llm):
    # deps.nli from the fixture is ML-disabled (.available False): the NLI key is skipped, not failed
    fake_llm.script("verify", {"checks": [{"index": 0, "supported": True, "reason": ""}]})
    out = await verify(_judge(("A 3% DA hike was approved.", ["E1"])), true_items(), deps)
    assert out.ok and out.methods == ["deterministic", "llm"]
    assert out.checks[0].nli_entailment is None


async def test_verifier_fails_closed_when_llm_verifier_fails(deps, fake_llm):
    deps.nli = entail(0.99)
    fake_llm.script("verify", ToolCallFailed(_err("llm_primary")))
    out = await verify(_judge(("A 3% DA hike was approved.", ["E1"])), true_items(), deps)
    assert not out.ok and "LLM verifier unavailable" in out.checks[0].reason


async def test_verifier_llm_omits_a_sentence(deps, fake_llm):
    deps.nli = entail(0.99)
    fake_llm.script("verify", {"checks": [{"index": 0, "supported": True, "reason": ""}]})
    judge = _judge(("A 3% DA hike was approved.", ["E1"]), ("Allowance rises to 63%.", ["E3"]))
    out = await verify(judge, true_items(), deps)
    assert [c.ok for c in out.checks] == [True, False]
    assert "no verdict" in out.checks[1].reason


async def test_verifier_only_sends_deterministically_valid_sentences_to_llm(deps, fake_llm):
    deps.nli = entail(0.99)
    fake_llm.script("verify", {"checks": [{"index": 1, "supported": True, "reason": ""}]})
    judge = _judge(("Invented source.", ["E77"]), ("A 3% DA hike was approved.", ["E1"]))
    out = await verify(judge, true_items(), deps)
    sent = fake_llm.calls_for("verify")[0].user
    assert "SENTENCE 1:" in sent and "SENTENCE 0:" not in sent
    assert [c.ok for c in out.checks] == [False, True]


async def test_verifier_empty_rationale_is_not_ok(deps):
    out = await verify(_judge(), true_items(), deps)
    assert not out.ok and out.failing_sentences


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ('PIB: "Cabinet approves 3% hike in Dearness Allowance"', True),
        ('PIB: "Cabinet approves 30% hike in Dearness Allowance"', False),  # altered quote
        ("PIB: Cabinet approves a hike", False),  # no quoted span at all
    ],
)
async def test_extractive_exact_quote_key(deps, text, ok):
    deps.nli = entail(0.9)
    out = await verify(_judge((text, ["E1"]), extractive=True), true_items(), deps)
    assert out.ok is ok
    assert "llm" not in out.methods


# ---- helpers -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("E3", "E3"),
        ("[e3]", "E3"),
        ("E03", "E3"),
        (" P12 ", "P12"),
        ("D1.", "D1"),
        ("X3", None),
        ("E3a", None),
    ],
)
def test_normalize_id(raw, expected):
    assert normalize_id(raw) == expected


def test_inline_ids_and_hypothesis_text():
    assert inline_ids("PIB and The Hindu confirm it [E1, E2] (P3)") == ["E1", "E2", "P3"]
    assert hypothesis_text("PIB confirms the hike [E1, E2].") == "PIB confirms the hike."
    assert (
        hypothesis_text("According to E4 the rating is False (E4; D2)") == "According to the rating is False"
    )


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("According to PIB, the Cabinet approved a 3% DA hike [E1].", "The Cabinet approved a 3% DA hike."),
        ("The Hindu and Moneycontrol report that DA rises to 63% (E2, E3)", "DA rises to 63%"),
        ("PIB announced a 3% increase in Dearness Allowance.", "A 3% increase in Dearness Allowance."),
        ("Factly rates the claim False [E4].", "Factly rates the claim False."),  # no attribution verb
        ("PIB says so [E1].", "PIB says so."),  # nothing left after stripping: kept as written
    ],
)
def test_nli_hypothesis_strips_attribution(sentence, expected):
    from veriloop.services.debate.verifier import nli_hypothesis

    assert nli_hypothesis(sentence) == expected


def test_nli_hypothesis_strips_multi_source_attribution():
    from veriloop.services.debate.verifier import nli_hypothesis

    assert nli_hypothesis("PIB, The Hindu and Moneycontrol report a 3% DA hike [E1, E2].") == "A 3% DA hike."


async def test_followup_only_for_a_thin_side(deps, fake_llm, settings, monkeypatch):
    """A side that already has >= 2 independent groups gets no follow-up (latency); a thin side does."""
    deps.nli = entail(0.9)
    calls: list[str] = []

    async def fake(query, deps, *, start_index=1):
        calls.append(query)
        return []

    monkeypatch.setattr(service, "followup_search", fake)
    script_debate(fake_llm, pro_query="pro q", con_query="con q")
    ext, fusion, items, fx, prov = setup(settings)
    assert fusion.groups_support >= 2 and fusion.groups_refute == 0
    result = await run_debate(ext, fusion, items, fx, prov, deps)
    assert calls == ["con q"]  # proponent's side is strong: its request is not granted
    assert result.proponent.followup_queries == [] and result.opponent.followup_queries == ["con q"]
