"""Lexicons (rating normalisation, verdict words, hedges) and query planning (E9)."""

from __future__ import annotations

import pytest

from veriloop.contracts import Claim, ClaimExtraction, Entity
from veriloop.services.evidence.lexicon import (
    Verdict,
    detect_verdict,
    has_manipulation_words,
    is_hedged,
    normalize_rating,
)
from veriloop.services.evidence.text import (
    claim_similarity,
    google_news_locale,
    keywords,
    plan_queries,
    relevance,
)


@pytest.mark.parametrize(
    ("rating", "value", "manip"),
    [
        ("False", -1.0, False),
        ("FALSE.", -1.0, False),
        ("Fact Crescendo Rating: False", -1.0, False),
        ("Pants on Fire", -1.0, False),
        ("Mostly False", -0.8, False),
        ("Misleading", -0.6, False),
        ("Missing Context", -0.5, False),
        ("Half True", 0.0, False),
        ("Mostly True", 0.6, False),
        ("True", 1.0, False),
        ("Not true", -1.0, False),
        ("Altered", -1.0, True),
        ("Deepfake", -1.0, True),
        ("AI-generated", -1.0, True),
        ("Edited video", -1.0, True),
        ("Satire", -0.8, False),
        ("Falso", -1.0, False),
        ("भ्रामक", -0.6, False),
        ("झूठ", -1.0, False),
        ("फर्जी", -1.0, False),
        ("डीपफेक", -1.0, True),
        ("نادرست", -1.0, False),
        ("unrated", 0.0, False),
        ("", 0.0, False),
    ],
)
def test_normalize_rating(rating, value, manip):
    norm = normalize_rating(rating)
    assert (norm.value, norm.manipulation) == (value, manip)


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Video of Blumenthal calling India a failed state is a deepfake", Verdict.MANIPULATED),
        ("Doctored video falsely shows US Senator", Verdict.MANIPULATED),
        ("Old video falsely shared as Cyclone Arnab", Verdict.FALSE),
        ("Old video from US territory linked to cyclone", Verdict.MISLEADING),
        ("No, PM did not announce free laptops", Verdict.FALSE),
        ("यह वीडियो फर्जी है", Verdict.FALSE),
        ("भ्रामक दावा वायरल", Verdict.MISLEADING),
        ("Cabinet approves 3% DA hike", None),
    ],
)
def test_detect_verdict(text, verdict):
    found = detect_verdict(text)
    assert (found.verdict if found else None) == verdict
    if found:
        assert found.word in text


@pytest.mark.parametrize(
    ("text", "hedged"),
    [
        ("8th Pay Commission fitment factor may hit 2.86 if govt agrees", True),
        ("Will it be 2.57, 2.86 or 4.0? Here's what experts say", True),
        ("DA hike likely next week", True),
        ("Did Rajnath Singh accuse soldiers?", True),
        ("फिटमेंट फैक्टर 2.86 हो सकता है", True),
        ("Cabinet approves 3% DA hike for central employees", False),
        ("DA hike announced on 28 May for employees", False),  # month, not the verb
        ("Arrears from May 2026 to be paid", False),
        ("Revised DA effective in May", False),
    ],
)
def test_is_hedged(text, hedged):
    assert is_hedged(text) is hedged


def test_has_manipulation_words():
    assert has_manipulation_words("AI-altered clip") and not has_manipulation_words("altercation in court")


def test_similarity_rescaling():
    assert claim_similarity(0.89) == pytest.approx(0.6)
    assert claim_similarity(0.9275) == pytest.approx(0.85)
    assert claim_similarity(0.5) == 0.0 and claim_similarity(1.0) == 1.0
    assert relevance(0.75) == 0.0 and relevance(0.90) == pytest.approx(1.0)


def test_google_news_locale():
    assert google_news_locale("en-IN") == ("en-IN", "IN", "IN:en")
    assert google_news_locale("hi-IN") == ("hi", "IN", "IN:hi")
    assert google_news_locale("garbage") == ("en-IN", "IN", "IN:en")


def test_keywords_drop_stopwords_keep_numbers():
    assert keywords("The Union Cabinet approved a 3% hike in DA on 28 October 2026") == [
        "Union",
        "Cabinet",
        "approved",
        "3%",
        "hike",
        "DA",
        "28",
        "October",
        "2026",
    ]


def _ext(text: str, text_en: str | None, language: str, entities=()) -> ClaimExtraction:
    return ClaimExtraction(
        language=language,
        claims=[Claim(id="C1", text=text, text_en=text_en, entities=list(entities))],
        primary_claim_id="C1",
        check_worthiness=0.9,
    )


def test_plan_queries_english_government_claim():
    plan = plan_queries(
        _ext(
            "The Union Cabinet approved a 3% hike in Dearness Allowance.",
            None,
            "en",
            [Entity(name="Union Cabinet", type="org")],
        ),
        None,
        "en-IN",
    )
    assert plan.government and plan.native is None
    assert plan.fact_check == "Union Cabinet fact check"
    assert "Dearness" in plan.main


def test_plan_queries_hindi_claim_searches_both_languages():
    plan = plan_queries(
        _ext(
            "केंद्रीय मंत्रिमंडल ने महंगाई भत्ते में 3 प्रतिशत की बढ़ोतरी को मंजूरी दी",
            "The Union Cabinet approved a 3 percent DA hike",
            "hi",
        ),
        None,
        "hi-IN",
    )
    assert plan.claim_en.startswith("The Union Cabinet") and "Cabinet" in plan.main
    assert plan.native and "मंत्रिमंडल" in plan.native
    assert plan.native_fact_check.endswith("फैक्ट चेक") and plan.native_locale == "hi-IN"
    assert plan.government


def test_plan_queries_without_claim():
    assert plan_queries(ClaimExtraction(), None, "en-IN") is None


@pytest.mark.parametrize(
    ("pp", "qp", "matched"),
    [  # e5 cosines measured 2026-10-31 (docs/reasoning.md "Similarity scales")
        (0.9616, 0.8736, True),  # same claim reworded
        (0.9434, 0.8638, True),  # paraphrase
        (0.8647, 0.9119, True),  # same claim, Hindi vs English (only the query-passage scale sees it)
        (0.8884, 0.8192, False),  # same topic, different event (state DA hike)
        (0.8757, 0.8404, False),  # speculation about the same topic
        (0.7917, 0.6951, False),  # unrelated
        (None, None, False),
    ],
)
def test_claim_match_takes_the_max_of_both_e5_scales(pp, qp, matched):
    from veriloop.services.evidence.text import claim_match

    assert (claim_match(pp, qp) >= 0.6) is matched


def test_match_similarity_uses_best_claim_text():
    import numpy as np

    from veriloop.services.evidence.claimreview import match_similarity

    review = np.zeros(384, dtype=np.float32)
    review[0] = 1.0
    other = np.zeros(384, dtype=np.float32)
    other[1] = 1.0
    q = np.stack([other, review * 0.0 + review])  # second claim text (e.g. the native one) matches
    assert match_similarity(review, q, None) == 1.0
    assert match_similarity(review, None, None) == 0.0
