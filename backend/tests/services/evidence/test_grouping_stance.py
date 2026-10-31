"""Independence grouping (G4), source lists, and the stance step (exact quotes, hedge caps, fallbacks)."""

from __future__ import annotations

import numpy as np
import pytest

from veriloop.contracts import ClaimReviewInfo, RetrievalMethod, Stance, ToolError
from veriloop.core.errors import ToolCallFailed
from veriloop.core.llm import DisabledLLM
from veriloop.core.ml import NLIScore
from veriloop.core.testing import FakeNLI
from veriloop.services.evidence import stance as st
from veriloop.services.evidence.grouping import assign_groups, collapse_near_duplicates, wire_from_dateline
from veriloop.services.evidence.lists import (
    factchecker_for,
    is_government,
    publisher_group,
    registrable_domain,
)
from veriloop.services.evidence.models import Candidate


def cand(item_id: str, host: str, title: str, snippet: str | None = None, **kw) -> Candidate:
    c = Candidate(
        source_name=kw.pop("source_name", host),
        url=kw.pop("url", f"https://{host}/{item_id.lower()}"),
        host=host,
        title=title,
        snippet=snippet if snippet is not None else title,
        retrieval_method=RetrievalMethod.GOOGLE_NEWS_RSS,
        tool=kw.pop("tool", "gnews_rss"),
        **kw,
    )
    c.item_id = item_id
    return c


# ---- lists ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("host", "group"),
    [
        ("economictimes.indiatimes.com", "times_group"),
        ("timesofindia.indiatimes.com", "times_group"),
        ("www.moneycontrol.com", "network18_reliance"),
        ("news18.com", "network18_reliance"),
        ("pib.gov.in", "govt_india"),
        ("www.pib.gov.in", "govt_india"),
        ("indianarmy.nic.in", "govt_india"),
        ("msn.com", "aggregator:msn"),
        ("unknown-blog.co.uk", "unknown-blog.co.uk"),
    ],
)
def test_publisher_group(host, group):
    assert publisher_group(host) == group


def test_registrable_domain_and_government_flag():
    assert registrable_domain("https://www.economictimes.indiatimes.com/x") == "indiatimes.com"
    assert registrable_domain("news.bbc.co.uk") == "bbc.co.uk"
    assert is_government("https://www.pib.gov.in/PressReleasePage.aspx?PRID=1")
    assert not is_government("thehindu.com")


@pytest.mark.parametrize(
    ("host", "url", "title", "is_fc"),
    [
        ("factly.in", "", "Anything", True),
        ("thequint.com", "https://www.thequint.com/news/politics/x", "Cabinet news", False),  # mixed outlet
        ("thequint.com", "https://www.thequint.com/news/webqoof/old-video", "Old video", True),
        ("indiatoday.in", "", "Fact Check: viral clip is edited", True),
        ("thehindu.com", "", "Fact check: claim", False),  # not a listed fact-checker
    ],
)
def test_factchecker_for(host, url, title, is_fc):
    assert (factchecker_for(host, url=url, title=title) is not None) is is_fc


# ---- grouping ------------------------------------------------------------------------------------
def test_wire_dateline_collapses_syndicated_copy_but_not_photo_credit():
    assert wire_from_dateline("NEW DELHI (PTI): The Cabinet approved") == "pti"
    assert wire_from_dateline("Photo credit (PTI) Photo") is None
    a = cand("E1", "thehindu.com", "DA hike", "New Delhi (PTI) The Cabinet approved a DA hike")
    b = cand("E2", "deccanherald.com", "DA hike", "New Delhi (PTI) The Cabinet approved a DA hike")
    c = cand("E3", "moneycontrol.com", "DA hike", "Cabinet approves 3% DA hike")
    assign_groups([a, b, c])
    assert a.group == b.group == "pti"
    assert c.group == "network18_reliance"
    assert {a.domain, b.domain} == {"thehindu.com", "deccanherald.com"}


def test_msn_copy_is_attributed_to_the_named_publisher():
    a = cand("E1", "thehindu.com", "DA hike", source_name="The Hindu")
    b = cand("E2", "msn.com", "DA hike", source_name="The Hindu on MSN")
    c = cand("E3", "msn.com", "DA hike", source_name="MSN")
    assign_groups([a, b, c])
    assert b.group == a.group == "the_hindu_group"
    assert c.group == "aggregator:msn"  # nameless aggregator copy stays uncounted


BODY = "The Union Cabinet on Tuesday approved a 3 per cent hike in dearness allowance for central staff."


def _same_vector(*cands: Candidate) -> None:
    v = np.ones(384, dtype=np.float32) / np.sqrt(384)
    for x in cands:
        x.embedding = v  # cosine 1.0 for every pair: only the other guards decide


def test_near_duplicate_snippets_collapse_paraphrases_do_not():
    a = cand("E1", "outlet-a.com", "DA hike", f"DA hike. {BODY}")
    b = cand("E2", "outlet-b.com", "Centre raises DA", f"Centre raises DA. {BODY} Arrears follow.")
    c = cand(
        "E3",
        "outlet-c.com",
        "DA up",
        "DA up. Employees will get three percent more allowance from July onwards.",
    )
    _same_vector(a, b, c)
    assign_groups([a, b, c])
    assert collapse_near_duplicates([a, b, c]) == 1
    assert a.group == b.group == "outlet-a.com"  # syndicated body text without a marker
    assert c.group == "outlet-c.com"  # independent paraphrase keeps its group


def test_headline_only_items_never_collapse():
    # Google News items carry only the headline; outlets reporting one announcement share wording.
    a = cand(
        "E1",
        "thehindu.com",
        "Cabinet approves 3% hike in Dearness Allowance for central government employees",
    )
    b = cand(
        "E2",
        "moneycontrol.com",
        "Cabinet approves 3% hike in Dearness Allowance for central government staff",
    )
    _same_vector(a, b)
    assign_groups([a, b])
    assert collapse_near_duplicates([a, b]) == 0
    assert a.group != b.group


def test_government_release_is_not_merged_with_a_report_of_it():
    pib = cand("E1", "pib.gov.in", "Cabinet approves DA", f"Cabinet approves DA. {BODY}", is_government=True)
    hindu = cand("E2", "thehindu.com", "Cabinet approves DA", f"Cabinet approves DA. {BODY}")
    _same_vector(pib, hindu)
    assign_groups([pib, hindu])
    assert collapse_near_duplicates([pib, hindu]) == 0
    assert (pib.group, hindu.group) == ("govt_india", "the_hindu_group")


# ---- stance -----------------------------------------------------------------------------------------
def test_locate_quote_is_tolerant_but_returns_exact_snippet_span():
    snippet = "Cabinet approves 3% hike in DA — “a big relief”, says minister."
    assert st.locate_quote("Cabinet approves 3% hike", snippet) == "Cabinet approves 3% hike"
    found = st.locate_quote('cabinet  APPROVES 3% hike in da - "a big relief"', snippet)
    assert found == "Cabinet approves 3% hike in DA — “a big relief"  # outer quote marks are trimmed
    assert found in snippet
    assert st.locate_quote("Cabinet rejects the hike", snippet) is None
    assert st.locate_quote("abc", snippet) is None


def _stance_cands() -> list[Candidate]:
    return [
        cand("E1", "pib.gov.in", "Cabinet approves 3% hike in Dearness Allowance"),
        cand("E2", "livemint.com", "Fitment factor may hit 2.86 if govt agrees"),
        cand(
            "E3",
            "thehindu.com",
            "Cabinet approves 3% DA hike",
            "The Cabinet approved a 3% DA hike on Tuesday.",
        ),
        cand("E4", "factly.in", "Old video falsely shared as DA protest", is_fact_check=True),
    ]


async def test_llm_stance_requires_exact_quotes_and_caps_hedges(deps, fake_llm):
    fake_llm.script(
        "stance",
        {
            "items": [
                {"id": "E1", "stance": "support", "confidence": 0.9, "quote": "Cabinet approves 3% hike"},
                {"id": "E2", "stance": "support", "confidence": 0.8, "quote": "Fitment factor may hit 2.86"},
                {"id": "[E3]", "stance": "support", "confidence": 0.9, "quote": "approved a 4% DA hike"},
                {"id": "E4", "stance": "refute", "confidence": 0.9, "quote": "falsely shared"},
                {"id": "E99", "stance": "refute", "confidence": 0.9, "quote": "x"},
            ]
        },
    )
    cands = _stance_cands()
    notes, method = await st.classify(deps, "Cabinet approved a 3% DA hike", cands)
    assert method == "llm"
    assert [(c.stance, c.stance_quote) for c in cands] == [
        (Stance.SUPPORT, "Cabinet approves 3% hike"),
        (Stance.NEUTRAL, None),  # hedged ("may") -> capped
        (Stance.NEUTRAL, None),  # quote not in snippet -> dropped
        (Stance.REFUTE, "falsely shared"),  # fact-check verdict word survives the cap
    ]
    assert any("not exact substrings" in n for n in notes)
    call = fake_llm.calls_for("stance")[0]
    assert call.effort == "none" and "Never use your own knowledge" in call.system


async def test_stance_falls_back_to_nli_when_llm_fails(deps, fake_llm):
    fake_llm.script(
        "stance", ToolCallFailed(ToolError(tool="llm_primary", error_type="timeout", message="x"))
    )

    def nli(premise: str, _h: str) -> NLIScore:
        if "approves" in premise or "approved" in premise:
            return NLIScore(entailment=0.9, neutral=0.05, contradiction=0.05)
        return NLIScore(entailment=0.1, neutral=0.8, contradiction=0.1)

    deps.nli = FakeNLI(nli)
    cands = _stance_cands()
    notes, method = await st.classify(deps, "Cabinet approved a 3% DA hike", cands)
    assert method == "nli" and any("fell back to local stance" in n for n in notes)
    assert cands[0].stance is Stance.SUPPORT and cands[0].stance_quote in cands[0].snippet
    assert cands[1].stance is Stance.NEUTRAL


async def test_slow_stance_batch_falls_back_locally_others_keep_llm(deps, fake_llm, monkeypatch):
    """Batches run in parallel; one that exceeds LLM_TIMEOUT_S gets local stance, the rest keep the LLM's."""
    monkeypatch.setattr(st, "LLM_BATCH", 2)
    monkeypatch.setattr(st, "LLM_TIMEOUT_S", 0.2)

    def respond(call):
        if "[E3]" in call.user:  # second batch (E3, E4): simulate a slow upstream
            raise TimeoutError
        return {
            "items": [
                {"id": "E1", "stance": "support", "confidence": 0.9, "quote": "Cabinet approves 3% hike"},
                {"id": "E2", "stance": "neutral", "confidence": 0.5, "quote": ""},
            ]
        }

    fake_llm.script("stance", respond)
    deps.nli = FakeNLI(
        lambda p, _h: NLIScore(entailment=0.9 if "approved" in p else 0.1, neutral=0.1, contradiction=0.0)
    )
    cands = _stance_cands()
    notes, method = await st.classify(deps, "Cabinet approved a 3% DA hike", cands)
    assert method == "llm+nli"
    assert len(fake_llm.calls_for("stance")) == 2
    assert cands[0].stance is Stance.SUPPORT  # from the LLM batch
    assert cands[2].stance is Stance.SUPPORT and cands[2].stance_quote in cands[2].snippet  # local NLI
    assert any("timeout" in n and "2 item(s)" in n for n in notes)


async def test_stance_lexicon_only_without_llm_and_nli(deps):
    deps.llm = DisabledLLM()  # deps.nli from the fixture is ML-disabled
    review = cand(
        "E1", "factly.in", "Fact check", "Claim reviewed: x — Rating: False (Factly)", is_fact_check=True
    )
    review.claimreview = ClaimReviewInfo(
        publisher="Factly", rating_text="False", normalized_rating=-1.0, similarity=0.8
    )
    news = cand("E2", "thehindu.com", "Cabinet approves 3% DA hike")
    _notes, method = await st.classify(deps, "claim", [review, news])
    assert method == "lexicon"
    assert review.stance is Stance.REFUTE and review.stance_quote in review.snippet
    assert news.stance is Stance.UNKNOWN  # never guessed
