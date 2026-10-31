"""Extractive claim splitting + check-worthiness heuristics (LLM disabled or failed; D-01b).

Deliberately simple and explainable: a sentence is check-worthy when it asserts something
verifiable (numbers, dates, named entities, event verbs, "video shows") and is not a greeting,
recipe, promotion, question or first-person opinion (E3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from langdetect import DetectorFactory, LangDetectException, detect_langs

DetectorFactory.seed = 0  # langdetect is randomised by default; make it deterministic

_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+|\n+")
_URL = re.compile(r"https?://\S+")
_HASHTAG_ONLY = re.compile(r"^(#\w+\s*)+$")

_EVENT_WORDS = re.compile(
    r"\b(killed|dead|died|death|hit|hits|struck|attack(ed|s)?|launch(ed|es)?|approv(ed|es)|announc(ed|es)|"
    r"arrest(ed|s)?|declar(ed|es)|ban(ned|s)?|crash(ed|es)?|flood(ed|s)?|cyclone|earthquake|fire|"
    r"elect(ed|ion)|won|wins|lost|resign(ed|s)?|said|says|claims?|shows?|showing|reveal(ed|s)?|"
    r"confirm(ed|s)?|hike|cut|rais(ed|es)|increas(ed|es)|decreas(ed|es)|merg(ed|er)|notification|"
    r"missile|strike|protest(ed|s)?|deepfake|viral|video|photo|image|footage)\b",
    re.IGNORECASE,
)
_HINDI_EVENT = re.compile(r"(हमला|मौत|मारे|घोषणा|मंजूरी|गिरफ्तार|वीडियो|तस्वीर|बढ़ोतरी|प्रतिशत|सरकार|चक्रवात)")
_NUMBER = re.compile(r"\d")
_YEAR_OR_DATE = re.compile(
    r"\b(19|20)\d{2}\b|\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I
)
_PERCENT_OR_MONEY = re.compile(r"(\d+(\.\d+)?\s?%|₹|rs\.?\s?\d|\$\s?\d|crore|lakh|million|billion)", re.I)

# E3: content that is not a factual claim.
_GREETING = re.compile(
    r"\b(good (morning|night|evening)|happy (diwali|holi|birthday|new year|eid|independence day)|"
    r"congratulations|best wishes|thank you|thanks for watching|have a (nice|great) day)\b",
    re.IGNORECASE,
)
_RECIPE = re.compile(
    r"\b(recipe|ingredients?|tbsp|tsp|teaspoon|tablespoon|preheat|bake|stir|simmer|saute|sauté|"
    r"chop(ped)?|knead|marinate|garnish|cup of|grams? of|minutes? until|dough|batter)\b",
    re.IGNORECASE,
)
_PROMO = re.compile(
    r"\b(subscribe|follow (us|me)|like and share|link in bio|use code|discount|buy now|giveaway|"
    r"dm (me|us)|check out my|new video|watch till the end)\b",
    re.IGNORECASE,
)
_OPINION = re.compile(
    r"\b(i think|i feel|i believe|in my opinion|i love|i hate|i like|imo|so beautiful|so cute|"
    r"amazing|awesome|beautiful|delicious|yummy|lol|lmao|omg)\b",
    re.IGNORECASE,
)
_TITLE_PERSON = re.compile(
    r"\b(senator|minister|pm|prime minister|president|chief minister|cm|governor|gen\.?|general|"
    r"lt\.?\s?gen\.?|actor|actress|mp|mla|dr\.?|mr\.?|mrs\.?|ms\.?|shri|smt\.?|justice|judge|"
    r"ceo|chairman|spokesperson|cricketer|singer|journalist)\s+((?:[A-Z][\w'.-]+\s?){1,3})",
    re.IGNORECASE,
)
_CAPS_SEQ = re.compile(r"\b([A-Z][a-zA-Z'’.-]+(?:\s+(?:of|the|de|van|al)?\s*[A-Z][a-zA-Z'’.-]+){0,3})")
_STOP_CAPS = {
    "The", "This", "That", "These", "Those", "A", "An", "In", "On", "At", "It", "He", "She", "They",
    "We", "I", "You", "Video", "Photo", "Watch", "Breaking", "Viral", "Fact", "Check", "Fake", "News",
    "Today", "Yesterday", "Here", "There", "Why", "What", "When", "How", "Who", "Did", "Does", "Is",
    "Are", "Was", "Were", "Please", "Share", "Must", "Also", "But", "And", "Or", "If", "As", "After",
}  # fmt: skip


# Honorifics/offices dropped from the front of a name so entity lookups (Wikidata) see the person.
_TITLE_WORDS = {
    "senator", "minister", "president", "pm", "cm", "governor", "gen", "general", "lt", "col", "maj",
    "major", "dr", "mr", "mrs", "ms", "shri", "smt", "justice", "judge", "actor", "actress", "mp", "mla",
    "chief", "prime", "sir",
}  # fmt: skip


@dataclass
class ScoredSentence:
    text: str
    score: float
    reasons: list[str] = field(default_factory=list)


def detect_language(text: str | None, default: str = "en") -> str:
    """ISO 639-1 code via langdetect; Devanagari-heavy text short-circuits to 'hi'."""
    if not text or len(text.strip()) < 3:
        return default
    letters = [c for c in text if c.isalpha()]
    if letters and sum("ऀ" <= c <= "ॿ" for c in letters) / len(letters) > 0.5:
        return "hi"
    try:
        langs = detect_langs(text)
    except LangDetectException:
        return default
    if not langs:
        return default
    code = langs[0].lang.split("-")[0]
    return code if langs[0].prob >= 0.6 else default


def split_sentences(text: str | None) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for raw in _SENTENCE_END.split(_URL.sub(" ", text)):
        sent = " ".join(raw.split()).strip(" -–—•|")
        if len(sent) < 12 or _HASHTAG_ONLY.match(sent):
            continue
        if sent not in out:
            out.append(sent)
    return out


def score_sentence(sentence: str) -> ScoredSentence:
    """0..1 check-worthiness of one sentence with human-readable reasons."""
    s = sentence.strip()
    score, reasons = 0.15, []
    if _EVENT_WORDS.search(s) or _HINDI_EVENT.search(s):
        score += 0.25
        reasons.append("event/assertion verb")
    if _NUMBER.search(s):
        score += 0.15
        reasons.append("number")
    if _YEAR_OR_DATE.search(s):
        score += 0.1
        reasons.append("date")
    if _PERCENT_OR_MONEY.search(s):
        score += 0.1
        reasons.append("statistic")
    if extract_entities(s):
        score += 0.2
        reasons.append("named entity")
    if re.search(r"\b(video|photo|image|clip|footage)\s+(shows|of)\b", s, re.I):
        score += 0.1
        reasons.append("describes media")
    penalties = (
        (_GREETING, 0.5, "greeting"),
        (_RECIPE, 0.5, "recipe/cooking"),
        (_PROMO, 0.4, "promotion"),
        (_OPINION, 0.3, "opinion"),
    )
    for pattern, weight, why in penalties:
        if pattern.search(s):
            score -= weight
            reasons.append(f"-{why}")
    if s.endswith("?"):
        score -= 0.2
        reasons.append("-question")
    if len(s.split()) < 4:
        score -= 0.15
        reasons.append("-too short")
    return ScoredSentence(s, round(max(0.0, min(1.0, score)), 3), reasons)


def extract_entities(text: str) -> list[str]:
    """Capitalised multi-word sequences not at a sentence start (cheap NER stand-in)."""
    names: list[str] = []
    for match in _CAPS_SEQ.finditer(text or ""):
        name = match.group(1).strip(" .,'’-")
        words = name.split()
        while words and words[0] in _STOP_CAPS:
            words = words[1:]
        if not words:
            continue
        titled = [i for i, w in enumerate(words) if w.lower().rstrip(".") in _TITLE_WORDS]
        if titled and titled[-1] < len(words) - 1:
            words = words[titled[-1] + 1 :]  # "US Senator Richard Blumenthal" -> "Richard Blumenthal"
        name = " ".join(words)
        if len(name) < 3 or (name.upper() == name and len(name) <= 3):
            continue
        if name not in names:
            names.append(name)
    return names[:12]


def person_subject(text: str) -> str | None:
    """A named person introduced with a title ('Senator Richard Blumenthal'), else None."""
    match = _TITLE_PERSON.search(text or "")
    if not match:
        return None
    # The pattern is case-insensitive (for the title), so keep only the capitalised name words.
    words: list[str] = []
    for word in match.group(2).split():
        if not word[0].isupper():
            break
        words.append(word.strip(".,"))
    return " ".join(words) or None


def non_claim_reason(text: str) -> str | None:
    """Why a whole post is not checkable (E3), or None."""
    for pattern, why in (
        (_RECIPE, "cooking/recipe content"),
        (_GREETING, "a greeting"),
        (_PROMO, "promotional content"),
    ):
        if len(pattern.findall(text or "")) >= 2 or (pattern.search(text or "") and len(text) < 120):
            return why
    return None
