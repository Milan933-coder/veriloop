"""Verifier regression on a realistic demo evidence set (Bing headline+snippet items, Google News
headline-only items, a question-form fact-check headline).

Before the fix, live runs rejected every rationale of cases (a) and (b): the LLM verifier was not shown
the item's source/date (so "The Financial Express reports ..." could not be confirmed) and NLI against
headline-length premises fell below 0.5 for correct sentences. The negatives below must still fail on
the local key alone, even when the LLM verifier says "supported" (dual key, D-13).
"""

# ruff: noqa: E501  (verbatim evidence strings from the live demo runs)
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from veriloop.contracts import (
    EvidenceItem,
    JudgeOutput,
    RationaleSentence,
    RetrievalMethod,
    Stance,
    VerdictLabel,
)
from veriloop.core.ml import NLIScore
from veriloop.core.testing import FakeNLI
from veriloop.services.debate import verify

OCT28 = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def _item(item_id, source, domain, group, title, snippet, *, tool="bing_news_rss", date=OCT28, gov=False):
    return EvidenceItem(
        id=item_id,
        source_name=source,
        url=f"https://{domain}/{item_id.lower()}",
        domain=domain,
        publisher_group=group,
        title=title,
        snippet=snippet,
        published_at=date,
        confidence_relevance=0.9,
        stance=Stance.SUPPORT,
        stance_confidence=0.9,
        stance_quote=title,
        retrieval_method=RetrievalMethod.GOOGLE_NEWS_RSS,
        tool=tool,
        is_government_source=gov,
    )


ITEMS = [
    _item(
        "E1",
        "The Financial Express",
        "financialexpress.com",
        "indian_express_group",
        "Cabinet approves 3% DA hike for central government employees",
        "Cabinet approves 3% DA hike for central government employees. The Union Cabinet approved a 3% Dearness "
        "Allowance hike for central government employees, raising the allowance to 63% of basic pay.",
    ),
    _item(
        "E2",
        "The Hindu",
        "thehindu.com",
        "the_hindu_group",
        "Cabinet approves 3% hike in dearness allowance for central government employees",
        "Cabinet approves 3% hike in dearness allowance for central government employees. Cabinet approves 3% "
        "DA/DR hike from July 1, 2026, benefiting over 1.17 crore central government employees and pensioners.",
    ),
    _item(  # Google News: headline only, PIB RSS: no date
        "E3",
        "Press Information Bureau (PIB)",
        "pib.gov.in",
        "govt_india",
        "Cabinet approves 3% increase in Dearness Allowance to Central Government employees",
        "Cabinet approves 3% increase in Dearness Allowance to Central Government employees",
        tool="pib_rss",
        date=None,
        gov=True,
    ),
    _item(
        "E4",
        "Moneycontrol",
        "moneycontrol.com",
        "network18_reliance",
        "Cabinet approves 3% DA hike for central government employees; allowance rises to 63% of basic pay",
        "Cabinet approves 3% DA hike for central government employees; allowance rises to 63% of basic pay",
        tool="gnews_rss",
    ),
    _item(  # question-form fact-check headline (case b)
        "E10",
        "NewsMeter",
        "newsmeter.in",
        "newsmeter.in",
        "Fact Check: Video shows Cyclone Arnab hitting Andhra Pradesh coast? No, it’s from Odisha",
        "Fact Check: Video shows Cyclone Arnab hitting Andhra Pradesh coast? No, it’s from Odisha",
        tool="gnews_rss",
    ),
    _item(
        "E15",
        "Fact Crescendo",
        "factcrescendo.com",
        "factcrescendo.com",
        "Fact Check: Cyclone Arnab impact? No, viral video is old",
        "Fact Check: Cyclone Arnab impact? No, viral video is old. A video showing trees bending violently in strong winds is circulating on social media with claims that it shows Cyclone Arnab.",
    ),
]

GOOD = [  # sentences the live judge wrote (cases a and b) + a multi-item summary
    (
        "The Financial Express reports that the Union Cabinet approved a 3% Dearness Allowance increase for "
        "central government employees on 28 October 2026.",
        ["E1"],
    ),
    (
        "The Hindu reports a 3% DA/DR increase benefiting central government employees and pensioners, in a "
        "report dated 28 October 2026.",
        ["E2"],
    ),
    (
        "PIB, The Hindu and Moneycontrol report a 3% hike in dearness allowance for central government employees.",
        ["E3", "E2", "E4"],
    ),
    (
        "NewsMeter says the video does not show Cyclone Arnab hitting the Andhra Pradesh coast and is from Odisha.",
        ["E10"],
    ),
    (  # live run 2: NLI vetoed this (0.98) because it read the question headline as an assertion
        "NewsMeter says the video is old and does not show Cyclone Arnab hitting the Andhra Pradesh coast.",
        ["E10", "E15"],
    ),
]
BAD = [  # each must fail the local key even though the LLM key below says "supported"
    ("The Union Cabinet approved a 4% Dearness Allowance hike for central government employees.", ["E1"]),
    ("The Cabinet also doubled the house rent allowance and transport subsidy for pensioners.", ["E2"]),
    ("PIB announced the increase on 28 October 2026.", ["E3"]),  # PIB item is undated: date not in evidence
    ("The video shows Cyclone Arnab hitting the Andhra Pradesh coast.", ["E10"]),  # polarity flipped
]


def realistic_nli(premise: str, hypothesis: str) -> NLIScore:
    """Scores measured with mDeBERTa-v3-base-xnli on these shapes (docs/reasoning.md)."""
    h = hypothesis.lower()
    if "4%" in h:
        return NLIScore(entailment=0.0, neutral=0.001, contradiction=0.999)
    if "house rent" in h or "28 october" in h or "dated" in h:
        return NLIScore(entailment=0.0, neutral=0.997, contradiction=0.003)
    if "does not show" in h:
        return NLIScore(entailment=0.484, neutral=0.329, contradiction=0.187)
    if "shows cyclone arnab" in h:
        return NLIScore(entailment=0.01, neutral=0.09, contradiction=0.90)
    return NLIScore(entailment=0.956, neutral=0.044, contradiction=0.0)


def judge(sentences) -> JudgeOutput:
    return JudgeOutput(
        label=VerdictLabel.TRUE,
        rationale=[RationaleSentence(text=t, evidence_ids=ids) for t, ids in sentences],
        provider="fake",
        model_id="fake",
    )


def all_supported(n: int) -> dict:
    return {"checks": [{"index": i, "supported": True, "reason": "ok"} for i in range(n)]}


async def test_grounded_demo_rationales_pass(deps, fake_llm):
    deps.nli = FakeNLI(realistic_nli)
    fake_llm.script("verify", all_supported(len(GOOD)))
    out = await verify(judge(GOOD), ITEMS, deps)
    assert out.ok, [(c.text, c.reason) for c in out.checks if not c.ok]
    assert out.methods == ["deterministic", "llm", "nli", "lexical"]
    # the LLM verifier sees each item's source and publication date (tool metadata) in ONE call
    calls = fake_llm.calls_for("verify")
    assert len(calls) == 1
    assert "The Financial Express (financialexpress.com), published 28 October 2026" in calls[0].user
    assert "Press Information Bureau (PIB) (pib.gov.in):" in calls[0].user  # undated: no date shown
    # one NLI pair per sentence (cost on CPU), premise capped
    assert len(deps.nli.calls) == len(GOOD)
    assert all(len(p) <= 700 for p, _ in deps.nli.calls)


@pytest.mark.parametrize("bad", BAD, ids=["changed-number", "added-fact", "date-not-in-evidence", "polarity"])
async def test_ungrounded_sentences_fail_the_local_key_even_if_llm_says_supported(deps, fake_llm, bad):
    deps.nli = FakeNLI(realistic_nli)
    fake_llm.script("verify", all_supported(1))
    out = await verify(judge([bad]), ITEMS, deps)
    assert not out.ok
    assert out.checks[0].llm_supported is True and out.checks[0].reason


async def test_llm_key_still_rejects_on_its_own(deps, fake_llm):
    deps.nli = FakeNLI(realistic_nli)
    fake_llm.script("verify", {"checks": [{"index": 0, "supported": False, "reason": "wrong source"}]})
    out = await verify(judge([GOOD[0]]), ITEMS, deps)
    assert not out.ok and "wrong source" in out.checks[0].reason


@pytest.mark.ml
async def test_real_nli_on_the_regression_set(deps, fake_llm, monkeypatch):
    """Same set with the real local NLI model (ml marker: loads mDeBERTa from VERILOOP_HOME/models).
    Measures NLI quality, not latency: the time cap is lifted."""
    from veriloop.core.config import get_settings
    from veriloop.core.ml import NLI
    from veriloop.services.debate import verifier as vmod

    monkeypatch.setattr(vmod, "NLI_TIMEOUT_S", 300.0)

    deps.nli = NLI(get_settings())
    assert await deps.nli.load(), deps.nli.error
    fake_llm.script("verify", all_supported(len(GOOD)))
    out = await verify(judge(GOOD), ITEMS, deps)
    assert out.ok, [(c.text, c.nli_entailment, c.reason) for c in out.checks if not c.ok]
    for bad in BAD:
        fake_llm.script("verify", all_supported(1))
        res = await verify(judge([bad]), ITEMS, deps)
        assert not res.ok, (bad, res.checks[0].nli_entailment, res.checks[0].reason)


async def test_nli_timeout_falls_back_to_lexical_coverage(deps, fake_llm, monkeypatch):
    """Contention (weights paged out) must not block the job: past NLI_TIMEOUT_S the local key is the
    deterministic coverage check alone; grounded sentences pass, an added fact still fails."""
    import asyncio

    from veriloop.services.debate import verifier as vmod

    class SlowNLI(FakeNLI):
        async def score(self, pairs):
            await asyncio.sleep(1.0)
            return await super().score(pairs)

    monkeypatch.setattr(vmod, "NLI_TIMEOUT_S", 0.05)
    deps.nli = SlowNLI(realistic_nli)
    fake_llm.script("verify", all_supported(2))
    out = await verify(judge([GOOD[2], BAD[1]]), ITEMS, deps)
    assert "nli_timeout" in out.methods and "lexical" in out.methods
    assert [c.ok for c in out.checks] == [True, False]
    assert "NLI timed out" in out.checks[0].reason and out.checks[0].nli_entailment is None


def test_iso_published_date_in_sentence_counts_as_covered() -> None:
    """Live false negative (case a): the judge cited the publish date as 2026-10-28 and the number
    check flagged the month '10' as invented. Tool-returned dates in either form are covered."""
    from veriloop.services.debate.verifier import lexical_coverage

    item = _item(
        "E1", "The Financial Express", "financialexpress.com", "indian_express_group",
        "Cabinet approves 3% DA hike for central government employees",
        "The Union Cabinet approved a 3% Dearness Allowance hike for central government employees.",
    )
    sentence = (
        "The Financial Express, dated 2026-10-28, reports that the Union Cabinet approved a 3% "
        "Dearness Allowance hike for central government employees [E1]."
    )
    _, missing = lexical_coverage(sentence, ["E1"], {"E1": item})
    assert missing == []
    # A number that is in neither the text nor the metadata is still caught.
    _, missing = lexical_coverage(sentence.replace("3%", "4%"), ["E1"], {"E1": item})
    assert missing == ["4"]
