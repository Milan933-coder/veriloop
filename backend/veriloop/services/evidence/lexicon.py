"""Verdict / hedge lexicons and ClaimReview rating normalisation (pure functions, no I/O).

Sources: docs/research.md ("Verdict detection in titles and snippets", rating variety in the feed)
and data/lists/factcheckers.json `verdict_lexicon`. Used by the evidence branch (title/snippet verdict
detection for fact-check articles, hedge caps on stance) and by fusion (manipulation evidence).

Latin-script words are matched with word boundaries, case-insensitively. Indic scripts are matched as
substrings with only a leading boundary: Python's `\\b` splits Devanagari words at vowel signs, and
inflected forms ("फर्जी" -> "फर्जीवाड़ा") should still match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Appended to ClaimReviewInfo.rating_text when the rating was inferred from a title/snippet rather
# than published as schema.org ClaimReview (the contract has no "method" field; fusion reads this).
DETECTED_SUFFIX = " (detected in headline)"


class Verdict(StrEnum):
    FALSE = "false"
    MISLEADING = "misleading"
    MANIPULATED = "manipulated"
    TRUE = "true"
    SATIRE = "satire"


_REFUTE_LATIN = [
    r"false(?:ly)?",
    r"fake",
    r"fabricated",
    r"hoax",
    r"debunk(?:s|ed|ing)?",
    r"not true",
    r"untrue",
    r"baseless",
    r"no evidence",
    r"pants on fire",
    r"mostly false",
    r"scam",
    r"did not",
    r"didn['’]t",
    r"never said",
]
_MISLEADING_LATIN = [
    r"misleading",
    r"misrepresent(?:s|ed|ing)?",
    r"out of context",
    r"missing context",
    r"old (?:video|image|photo|clip|picture)",
    r"unrelated",
    r"falsely (?:shared|linked|claim(?:s|ed)?|attributed|viral)",
    r"misattributed",
    r"partly false",
    r"shared as",
    r"not from",
]
_MANIPULATED_LATIN = [
    r"deep\s?fakes?",
    r"ai[- ]generated",
    r"ai[- ](?:manipulated|altered|edited|made)",
    r"created (?:with|using) ai",
    r"made (?:with|using) ai",
    r"generated (?:with|by|using) ai",
    r"voice clon(?:e|ed|ing)",
    r"cloned voice",
    r"edited",
    r"doctored",
    r"morphed",
    r"digitally (?:altered|manipulated|edited)",
    r"photoshopped",
    r"synthetic",
    r"altered",
    r"manipulated",
]
_SATIRE_LATIN = [r"satire", r"satirical", r"parody", r"spoof"]

# Indic-script verdict words (research.md + factcheckers.json lexicon).
_REFUTE_INDIC = [
    "फर्जी",
    "फ़र्ज़ी",
    "फेक",
    "झूठा",
    "झूठी",
    "झूठ",
    "गलत",
    "ग़लत",
    "निराधार",
    "अफवाह",
    "తప్పు",
    "ఫేక్",
    "ভুয়া",
    "ভুয়ো",
    "মিথ্যা",
    "தவறு",
    "போலி",
]
_MISLEADING_INDIC = ["भ्रामक", "पुराना वीडियो", "पुरानी तस्वीर", "असंबंधित", "गुमराह", "বিভ্রান্তিকর"]
_MANIPULATED_INDIC = ["डीपफेक", "एआई से बना", "एआई से बनी", "एडिटेड", "छेड़छाड़", "मॉर्फ्ड"]

_HEDGE_LATIN = [
    # "may" the verb, not the month: "28 May", "May 2026", "in/on/of May" are dates, not hedges.
    r"(?<!\d )(?<!in )(?<!on )(?<!of )may(?!\s+\d)",
    r"might",
    r"could",
    r"likely",
    r"unlikely",
    r"expected",
    r"expects?",
    r"will it",
    r"experts? says?",
    r"possibly",
    r"reportedly",
    r"set to",
    r"proposed",
    r"speculat\w*",
    r"rumou?rs?",
    r"if",
    r"what if",
    r"calculat\w*",
]
_HEDGE_INDIC = ["हुआ तो", "हो सकता", "सकती है", "सकते हैं", "संभव", "अनुमान", "उम्मीद", "संभावना"]

_DEATH_WORDS = re.compile(r"\b(?:died|dies|death|dead|late|passed away|funeral|demise)\b|निधन|मौत|देहांत", re.I)


def _latin(words: list[str]) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])(?:" + "|".join(words) + r")(?![\w-])", re.IGNORECASE)


def _indic(words: list[str]) -> re.Pattern[str]:
    return re.compile(r"(?<![ऀ-෿])(?:" + "|".join(map(re.escape, words)) + ")")


_RE_REFUTE = _latin(_REFUTE_LATIN)
_RE_MISLEADING = _latin(_MISLEADING_LATIN)
_RE_MANIPULATED = _latin(_MANIPULATED_LATIN)
_RE_SATIRE = _latin(_SATIRE_LATIN)
_RE_REFUTE_I = _indic(_REFUTE_INDIC)
_RE_MISLEADING_I = _indic(_MISLEADING_INDIC)
_RE_MANIPULATED_I = _indic(_MANIPULATED_INDIC)
_RE_HEDGE = _latin(_HEDGE_LATIN)
_RE_HEDGE_I = _indic(_HEDGE_INDIC)


@dataclass(frozen=True)
class DetectedVerdict:
    verdict: Verdict
    word: str  # the matched span, verbatim from the text


def detect_verdict(text: str) -> DetectedVerdict | None:
    """Verdict words in a fact-check headline/snippet. Manipulation wins over false over misleading."""
    if not text:
        return None
    for verdict, patterns in (
        (Verdict.MANIPULATED, (_RE_MANIPULATED, _RE_MANIPULATED_I)),
        (Verdict.SATIRE, (_RE_SATIRE,)),
        (Verdict.FALSE, (_RE_REFUTE, _RE_REFUTE_I)),
        (Verdict.MISLEADING, (_RE_MISLEADING, _RE_MISLEADING_I)),
    ):
        for pattern in patterns:
            m = pattern.search(text)
            if m:
                return DetectedVerdict(verdict, m.group(0))
    return None


def has_manipulation_words(text: str) -> bool:
    return bool(text) and bool(_RE_MANIPULATED.search(text) or _RE_MANIPULATED_I.search(text))


def is_hedged(text: str) -> bool:
    """Speculation cues (may/could/expected/हो सकता है ...) or a question headline: cap stance at neutral."""
    if not text:
        return False
    stripped = text.strip()
    if stripped.endswith("?"):
        return True
    return bool(_RE_HEDGE.search(stripped) or _RE_HEDGE_I.search(stripped))


def mentions_death(text: str) -> bool:
    return bool(_DEATH_WORDS.search(text or ""))


# ---- ClaimReview rating normalisation -------------------------------------------------------------
# Ordered: the first matching entry wins, so negated / qualified phrases precede bare words.
_RATING_TABLE: list[tuple[re.Pattern[str], float]] = [
    (_latin([r"not true", r"untrue", r"not correct", r"incorrect", r"not accurate", r"inaccurate"]), -1.0),
    (_latin([r"mostly false", r"largely false", r"falso en su mayor[ií]a"]), -0.8),
    (_latin([r"mostly true", r"largely true", r"mostly accurate"]), 0.6),
    (_latin([r"half[- ]true", r"mixture", r"mixed", r"partly true", r"partially true", r"half truth"]), 0.0),
    (
        _latin(
            [
                r"partly false",
                r"partially false",
                r"missing context",
                r"needs context",
                r"lacks context",
                r"exaggerat\w*",
                r"cherry[- ]picks?",
                r"distort\w*",
            ]
        ),
        -0.5,
    ),
    (
        _latin(
            [
                r"misleading",
                r"out of context",
                r"misattributed",
                r"misrepresent\w*",
                r"engañoso",
                r"enganoso",
                r"trompeur",
                r"irreführend",
                r"fuorviante",
                r"sesat",
                r"menyesatkan",
            ]
        ),
        -0.6,
    ),
    (_latin([r"unproven", r"unverified", r"no evidence", r"unsupported", r"sin evidencia"]), -0.3),
    (_latin(_SATIRE_LATIN), -0.8),
    (_RE_MANIPULATED, -1.0),
    (
        _latin(
            [
                r"false",
                r"fake",
                r"fake news",
                r"hoax",
                r"fabricated",
                r"pants on fire",
                r"wrong",
                r"baseless",
                r"scam",
                r"falso",
                r"falsa",
                r"falsch",
                r"faux",
                r"fausse",
                r"salah",
                r"palsu",
                r"bohong",
                r"sai",
                r"rumou?r",
            ]
        ),
        -1.0,
    ),
    (
        _latin(
            [r"true", r"correct", r"accurate", r"verdadero", r"verdadeiro", r"vrai", r"richtig", r"benar"]
        ),
        1.0,
    ),
    (_RE_MANIPULATED_I, -1.0),
    (_indic(["भ्रामक", "गुमराह", "বিভ্রান্তিকর", "தவறான தகவல்"]), -0.6),
    (_indic(["आंशिक रूप से सही", "आधा सच"]), 0.0),
    (_RE_REFUTE_I, -1.0),
    (_indic(["نادرست", "کذب", "زائف", "مضلل"]), -1.0),
    (_indic(["सच", "सही", "సరైనది", "সত্য", "உண்மை"]), 1.0),
]
_PREFIX = re.compile(r"^[^:：]{0,40}rating\s*[:：]\s*", re.IGNORECASE)


@dataclass(frozen=True)
class NormalizedRating:
    value: float  # -1 (false) .. +1 (true); 0 = mixed/unrated
    manipulation: bool
    matched: str | None  # lexicon span that decided it (None = unrated)


def clean_rating_text(rating: str) -> str:
    text = " ".join((rating or "").split())
    text = _PREFIX.sub("", text)  # "Fact Crescendo Rating: False" -> "False"
    if ":" in text and len(text.split(":", 1)[0]) <= 40:  # localized prefixes (e.g. Tamil "...: தவறு")
        head, tail = text.split(":", 1)
        if tail.strip() and normalize_rating(tail.strip(), _clean=False).matched:
            text = tail.strip()
        else:
            text = text if head else tail
    return text.strip().rstrip(".").strip()


def normalize_rating(rating: str, *, _clean: bool = True) -> NormalizedRating:
    """Map a textual ClaimReview rating ("False", "Altered", "भ्रामक", ...) to [-1, 1]."""
    text = clean_rating_text(rating) if _clean else (rating or "")
    # Long explanatory ratings: the verdict word is normally at the start.
    text = text[:160]
    manipulation = has_manipulation_words(text)
    for pattern, value in _RATING_TABLE:
        m = pattern.search(text)
        if m:
            return NormalizedRating(value, manipulation, m.group(0))
    return NormalizedRating(0.0, manipulation, None)


def verdict_to_rating(verdict: Verdict) -> float:
    return {
        Verdict.FALSE: -1.0,
        Verdict.MANIPULATED: -1.0,
        Verdict.MISLEADING: -0.6,
        Verdict.SATIRE: -0.8,
        Verdict.TRUE: 1.0,
    }[verdict]
