"""Text helpers: query building, keyword extraction, similarity rescaling, near-duplicate tests."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from veriloop.contracts import ClaimExtraction, ResolvedInput

# multilingual-e5 cosines are compressed into a narrow band, and on two different scales depending on
# the "query: "/"passage: " prefixes (measured 2026-10-31, docs/reasoning.md "Similarity scales"):
#                              passage-passage   query-passage
#   same claim, reworded            0.94-0.96          0.86-0.87
#   same claim, Hindi vs English    0.86               0.91
#   same topic, different event     0.89               0.82
#   same topic, speculation         0.88               0.84
#   unrelated                       0.78-0.79          0.70-0.75
# BUILD_SPEC thresholds (0.6 ClaimReview match, 0.85 fast path) assume a 0..1 similarity, so cosines
# are rescaled linearly per scale. Claim-vs-reviewed-claim is a symmetric task: `claim_match` takes the
# max of both scales (passage-passage separates paraphrases; query-passage keeps cross-lingual matches).
# The stance step is the second gate: a review of a different claim is "neutral" and never counts.
CLAIM_SIM_FLOOR, CLAIM_SIM_SPAN = 0.80, 0.15  # passage-passage: 0.89 -> 0.60, 0.9275 -> 0.85
CLAIM_QP_FLOOR, CLAIM_QP_SPAN = 0.78, 0.12  # query-passage: 0.852 -> 0.60, 0.882 -> 0.85
REL_FLOOR, REL_SPAN = 0.75, 0.15  # claim (query) vs headline/snippet (passage): 0.75 -> 0, 0.90 -> 1


def claim_similarity(cosine: float) -> float:
    """Passage-passage cosine -> 0..1."""
    return max(0.0, min(1.0, (cosine - CLAIM_SIM_FLOOR) / CLAIM_SIM_SPAN))


def claim_similarity_qp(cosine: float) -> float:
    """Query-passage cosine -> 0..1."""
    return max(0.0, min(1.0, (cosine - CLAIM_QP_FLOOR) / CLAIM_QP_SPAN))


def claim_match(pp_cosine: float | None, qp_cosine: float | None) -> float:
    """Similarity of our claim to a reviewed claim: max over the available scales."""
    scores = []
    if pp_cosine is not None:
        scores.append(claim_similarity(pp_cosine))
    if qp_cosine is not None:
        scores.append(claim_similarity_qp(qp_cosine))
    return max(scores, default=0.0)


def relevance(cosine: float) -> float:
    return max(0.0, min(1.0, (cosine - REL_FLOOR) / REL_SPAN))


_STOP_EN = frozenset(
    """a an the and or but of to in on at for with by from as is are was were be been being it its this
    that these those there their they them he she his her him i we you your our my me us do does did done
    has have had having will would shall should can could may might must not no nor so than then too very
    just also only into over under about after before during while since until again further once here
    when where why how all any both each few more most other some such own same what which who whom whose
    if because said says say according reportedly claims claim claimed shows show showing video photo
    image viral post shared new news today latest""".split()
)
_STOP_HI = frozenset(
    "है हैं था थे की के का को में से पर और या भी यह वह ये वो एक ने तो ही कि लिए साथ गया गई दिया".split()
)
# Python's \w excludes combining marks (category M), so Indic vowel signs / virama would split
# "मंत्रिमंडल" into fragments (E9 queries became garbage). Indic (U+0900-U+0DFF), Thai and Arabic
# diacritic ranges are added explicitly.
_TOKEN = re.compile(
    r"[\w\u0900-\u0DFF\u0E00-\u0E7F\u0610-\u061A\u064B-\u065F\u200c\u200d%\u20b9$.,'\u2019-]+",
    re.UNICODE,
)


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(html.unescape(text).split())


def tokens(text: str) -> list[str]:
    out = []
    for raw in _TOKEN.findall(text or ""):
        tok = raw.strip(".,'’-")
        if tok:
            out.append(tok)
    return out


def keywords(text: str, *, limit: int = 12) -> list[str]:
    """Content words in order (stopwords dropped, duplicates removed). Numbers and % are kept."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens(text):
        low = tok.lower()
        if low in _STOP_EN or low in _STOP_HI or (len(tok) < 2 and not tok.isdigit()):
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def jaccard(a: str, b: str) -> float:
    ta = {t.lower() for t in tokens(a)}
    tb = {t.lower() for t in tokens(b)}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def norm_title(title: str) -> str:
    return " ".join(t.lower() for t in tokens(title))


@dataclass
class QueryPlan:
    claim: str  # primary claim, original language
    claim_en: str  # English rendering used for English sources (E9)
    language: str
    locale: str
    main: str  # keyword query (English)
    fact_check: str  # fact-check flavoured variant
    entities: list[str] = field(default_factory=list)
    person_entities: list[str] = field(default_factory=list)
    native: str | None = None  # original-language keyword query when not English
    native_fact_check: str | None = None
    native_locale: str | None = None
    government: bool = False  # Indian-government claim -> PIB sources
    media_word: str | None = None
    claim_type: str = "event"


_GOVT = re.compile(
    r"\b(cabinet|union|central government|centre|govt|government of india|ministry|minister|pm modi|"
    r"prime minister|scheme|yojana|pib|rbi|reserve bank|gazette|notification|lok sabha|rajya sabha|"
    r"parliament|president of india|election commission|dearness allowance|pay commission|isro|army|navy|"
    r"air force|cbi|ed|income tax|gst|aadhaar|pension|epfo|upi)\b|"
    r"सरकार|मंत्रालय|मंत्रिमंडल|कैबिनेट|योजना|प्रधानमंत्री|महंगाई भत्ता",
    re.IGNORECASE,
)
_MEDIA = re.compile(r"\b(video|photo|image|picture|clip|audio|reel)\b", re.IGNORECASE)
_LOCALE_RE = re.compile(r"^([a-z]{2})(?:-([A-Z]{2}))?$")


def google_news_locale(locale: str) -> tuple[str, str, str]:
    """'en-IN' -> (hl='en-IN', gl='IN', ceid='IN:en'); 'hi-IN' -> ('hi', 'IN', 'IN:hi')."""
    m = _LOCALE_RE.match(locale or "")
    lang, country = (m.group(1), m.group(2) or "IN") if m else ("en", "IN")
    hl = f"{lang}-{country}" if lang == "en" else lang
    return hl, country, f"{country}:{lang}"


def plan_queries(
    extraction: ClaimExtraction, resolved: ResolvedInput | None, locale: str
) -> QueryPlan | None:
    claim = extraction.primary_claim()
    if claim is None:
        return None
    language = (extraction.language or "en").split("-")[0].lower()
    claim_en = claim.text_en or claim.text
    entities = [e.name for e in claim.entities if e.name][:4]
    persons = [e.name for e in claim.entities if e.name and e.type == "person"][:3]
    main = " ".join(keywords(claim_en, limit=12))
    fc_terms = entities or keywords(claim_en, limit=6)
    fact_check = " ".join(fc_terms[:6]) + " fact check"
    media = _MEDIA.search(f"{claim.text} {claim_en}")
    plan = QueryPlan(
        claim=claim.text,
        claim_en=claim_en,
        language=language,
        locale=locale or "en-IN",
        main=main or claim_en[:200],
        fact_check=fact_check,
        entities=entities,
        person_entities=persons,
        government=bool(_GOVT.search(f"{claim.text} {claim_en}")),
        media_word=media.group(1).lower() if media else None,
        claim_type=claim.claim_type or "event",
    )
    if language != "en":
        native_kw = keywords(claim.text, limit=12)
        plan.native = " ".join(native_kw) or claim.text[:200]
        plan.native_fact_check = " ".join(native_kw[:6]) + (" फैक्ट चेक" if language == "hi" else " fact check")
        country = google_news_locale(locale)[1]
        plan.native_locale = f"{language}-{country}"
    return plan
