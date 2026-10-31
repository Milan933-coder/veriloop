"""Dual-key grounding verifier (D-13, SPEC §5 "Verifier agent").

A rationale sentence passes only if ALL of these hold:
1. deterministic: it cites >= 1 id, every cited id (and every id written inline in the text) exists
   in this job (E/P/D items), and every URL in the text is the URL of an item of this job;
2. LLM key: ONE batched support check (effort low) says the cited evidence supports the sentence.
   The evidence shown is the tool data of each cited item: its header (source name, domain,
   publication date) and its title / snippet / rating. For an extractive judge (LLM disabled) this key
   is an exact-substring check of the quoted span instead;
3. local key (independent of the LLM), when NLI is available, scored on ONE (premise, hypothesis) pair
   per sentence (the cited items joined), in parallel with the LLM key:
   - pass when NLI entailment >= NLI_ENTAIL_MIN;
   - otherwise pass only when NLI finds no contradiction (< NLI_CONTRADICTION_VETO) AND the cited text
     covers the sentence deterministically: every number in the sentence appears in it and at least
     LEXICAL_COVERAGE_MIN of its content words do.
   Why the fallback: measured on this model (docs/reasoning.md), grounded sentences score 0.96-0.996,
   but a correct sentence against a question-form fact-check headline ("Video shows X? No, it's from
   Odisha") scored 0.48. Changed numbers / actors are contradictions (0.97-0.999) and stay rejected;
   an added fact is "neutral" for NLI but fails the coverage check.
Any failing sentence makes the verifier not ok; decide() then abstains (UNGROUNDED_RATIONALE).
If the LLM verifier itself fails, sentences cannot be confirmed and fail (never silently passed).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from veriloop.contracts import EvidenceItem, GroundingCheck, JudgeOutput, VerifierOutput
from veriloop.core.errors import LLMDisabled, MLUnavailable, ToolCallFailed
from veriloop.deps import Deps
from veriloop.services.debate import prompts
from veriloop.services.debate.schemas import SupportBatch
from veriloop.services.debate.table import inline_ids
from veriloop.services.evidence.text import tokens

log = structlog.get_logger(__name__)

_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
_BRACKETED_IDS = re.compile(r"\s*[\[(]\s*(?:[EPD]\d+\s*[,;/]?\s*(?:and\s+)?)+[\])]", re.IGNORECASE)
_QUOTED = re.compile(r'"([^"]{4,})"')
PREMISE_CHARS = 450  # NLI cost grows with premise length (CPU); headline + snippet start is enough
ITEM_CHARS = 600  # per cited item in the LLM verifier prompt
NLI_CONTRADICTION_VETO = 0.50
LEXICAL_COVERAGE_MIN = 0.75
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_STOP = frozenset(
    """a an the and or but of to in on at for with by from as is are was were be been being it its this that
    these those there their they them he she his her him we you your our not no nor so than then too very
    just also only into over under about after before during while since until has have had having will
    would shall should can could may might must do does did done says said say reports reported report
    according shows show showing claim claims claimed claiming per cent percent which who whom whose what
    when where why how all any both each more most other some such same own one""".split()
)


def _date_text(item: EvidenceItem) -> str:
    d = item.published_at
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


def _header(item: EvidenceItem) -> str:
    """Tool-returned metadata a sentence may legitimately rely on (who published it, and when)."""
    date = _date_text(item)
    if not date:
        return f"{item.source_name} ({item.domain})"
    # Judges cite dates as "28 October 2026" or "2026-10-28"; both come from tool metadata, so both
    # forms count as covered (otherwise the month "10" looks like an invented number).
    iso = item.published_at.date().isoformat() if item.published_at else ""
    return f"{item.source_name} ({item.domain}), published {date} ({iso})"


def _item_text(item: EvidenceItem) -> str:
    parts = [item.title or "", item.snippet]
    if item.claimreview is not None:
        parts.append(f"Rating: {item.claimreview.rating_text} ({item.claimreview.publisher}).")
    text = " ".join(p.strip() for p in parts if p and p.strip())
    if item.title and item.snippet.startswith(item.title):  # Bing/Google snippets repeat the headline
        text = " ".join(p.strip() for p in parts[1:] if p and p.strip())
    return text


def hypothesis_text(sentence: str) -> str:
    """Sentence without id brackets/inline ids, for NLI (ids are not language)."""
    text = _BRACKETED_IDS.sub("", sentence)
    text = re.sub(r"\b[EPD]\d+\b", "", text)
    return " ".join(text.split()).strip()


# "According to PIB, X" / "The Hindu and Moneycontrol report that X" -> "X". The cited headline states
# X, not who reported it, so NLI would score the attribution as unsupported; whether the attribution
# matches the cited item is checked by the LLM key (it sees each item's source) and by the coverage check.
_ATTRIBUTION = re.compile(
    r"^(?:according to [^,]{1,80},\s*|[^.;:]{1,100}?\b(?:reports?|reported|says?|said|states?|stated|"
    r"confirms?|confirmed|announces?|announced|notes?|noted|writes?|wrote)\s+(?:that\s+)?)",
    re.IGNORECASE,
)


def nli_hypothesis(sentence: str) -> str:
    text = hypothesis_text(sentence)
    stripped = _ATTRIBUTION.sub("", text, count=1).strip()
    if len(stripped) < 12:  # nothing left but the attribution: keep the sentence as written
        return text
    return stripped[0].upper() + stripped[1:]


# Fact-check headlines pose the claim as a question ("Video shows X? No, it's from Odisha"); NLI reads
# the question as an assertion of X and scores "the video does not show X" as a contradiction (0.98
# measured live). For the NLI premise only, the question is rewritten into the statement it makes.
_FC_LABEL = re.compile(
    r"(?:^|(?<=\.\s))\s*(?:#?fact[\s-]?check|factcheck|verified|webqoof)\s*[:|\-]\s*", re.I
)
_QUESTION_DENIED = re.compile(
    r"([^?.!]{8,}?)\?\s*(?:no|not really|false|fake|wrong|misleading)\b[\s,.!:;\-]*", re.IGNORECASE
)
_QUESTION = re.compile(r"([^?.!]{8,}?)\?")


def nli_readable(text: str) -> str:
    text = _FC_LABEL.sub("", text)
    text = _QUESTION_DENIED.sub(lambda m: f"It is not true that {m.group(1).strip()}. ", text)
    text = _QUESTION.sub(lambda m: f"It is claimed that {m.group(1).strip()}.", text)
    return " ".join(text.split())


def nli_premise(ids: Sequence[str], by_id: dict[str, EvidenceItem]) -> str:
    texts = []
    for i in ids:
        if i in by_id:
            date = _date_text(by_id[i])
            texts.append(nli_readable(_item_text(by_id[i])) + (f" Published on {date}." if date else ""))
    return " ".join(texts)[:PREMISE_CHARS]


def _content_words(text: str) -> list[str]:
    return [
        t.lower() for t in tokens(text) if len(t) > 2 and t.lower() not in _STOP and not _NUMBER.fullmatch(t)
    ]


def _covered(word: str, haystack: set[str]) -> bool:
    if word in haystack:
        return True
    stem = word[:5]  # crude stemming: approves/approved, increase/increased, falsely/false
    return len(stem) >= 4 and any(
        h.startswith(stem) or word.startswith(h[:5]) for h in haystack if len(h) >= 4
    )


def lexical_coverage(
    sentence: str, ids: Sequence[str], by_id: dict[str, EvidenceItem]
) -> tuple[float, list[str]]:
    """(share of the sentence's content words found in the cited items incl. headers, missing numbers)."""
    cited = " ".join(
        f"{_header(by_id[i])} {_item_text(by_id[i])} {by_id[i].stance_quote or ''}" for i in ids if i in by_id
    )
    hay_words = {t.lower() for t in tokens(cited)}
    hay_numbers = {n.replace(",", "") for n in _NUMBER.findall(cited)}
    hyp = hypothesis_text(sentence)
    missing_numbers = [n for n in _NUMBER.findall(hyp) if n.replace(",", "") not in hay_numbers]
    words = _content_words(hyp)
    if not words:
        return 1.0, missing_numbers
    hit = sum(1 for w in words if _covered(w, hay_words))
    return hit / len(words), missing_numbers


def deterministic_check(
    sentence: str, ids: Sequence[str], by_id: dict[str, EvidenceItem], job_urls: set[str]
) -> tuple[bool, str]:
    if not sentence.strip():
        return False, "empty sentence"
    if not ids:
        return False, "no evidence ids cited"
    missing = [i for i in ids if i not in by_id]
    if missing:
        return False, f"cited id(s) not in this job: {', '.join(missing)}"
    stray = [i for i in inline_ids(sentence) if i not in by_id]
    if stray:
        return False, f"id(s) written in the text do not exist: {', '.join(stray)}"
    bad_urls = [u.rstrip(".,;") for u in _URL.findall(sentence) if u.rstrip(".,;") not in job_urls]
    if bad_urls:
        return False, f"URL(s) not returned by any tool in this job: {', '.join(bad_urls)}"
    return True, ""


def quote_check(sentence: str, ids: Sequence[str], by_id: dict[str, EvidenceItem]) -> bool:
    """Extractive key: every quoted span appears verbatim in one of the cited items."""
    quotes = _QUOTED.findall(sentence)
    if not quotes:
        return False
    texts = [
        f"{by_id[i].title or ''} {by_id[i].snippet} {by_id[i].stance_quote or ''}" for i in ids if i in by_id
    ]
    return all(any(q.strip() in t for t in texts) for q in quotes)


async def _llm_support(
    deps: Deps, judge: JudgeOutput, indices: list[int], by_id: dict[str, EvidenceItem]
) -> tuple[dict[int, tuple[bool, str]], str | None]:
    """ONE batched call for all sentences. index -> (supported, reason); 2nd value = error message."""
    if not indices:
        return {}, None
    blocks = []
    for n in indices:
        s = judge.rationale[n]
        cited = "\n".join(
            f"  [{i}] {_header(by_id[i])}: {_item_text(by_id[i])[:ITEM_CHARS]}"
            for i in s.evidence_ids
            if i in by_id
        )
        blocks.append(f"SENTENCE {n}: {hypothesis_text(s.text)}\nCITED EVIDENCE:\n{cited}")
    try:
        out = await deps.llm.complete_json(
            task="verify",
            system=prompts.VERIFIER,
            user="\n\n".join(blocks),
            schema=SupportBatch,
            effort="low",
            max_tokens=1200,
        )
    except (LLMDisabled, ToolCallFailed) as exc:
        msg = exc.error.message if isinstance(exc, ToolCallFailed) else "LLM disabled"
        log.warning("verifier_llm_failed", error=msg[:200])
        return {}, msg
    result: dict[int, tuple[bool, str]] = {}
    for c in out.checks:
        if c.index in indices and c.index not in result:
            result[c.index] = (bool(c.supported), " ".join((c.reason or "").split())[:200])
    return result, None


@dataclass
class _NLIScore:
    entailment: float
    contradiction: float


NLI_TIMEOUT_S = 4.0  # runs in parallel with the LLM key (2.5-5 s), so it rarely adds latency
NLI_TIMED_OUT: dict[int, _NLIScore] = {}  # sentinel, compared by identity


async def _nli(
    deps: Deps, judge: JudgeOutput, indices: list[int], by_id: dict[str, EvidenceItem]
) -> dict[int, _NLIScore] | None:
    """One NLI pair per sentence (premise = cited items joined). None when NLI is unavailable."""
    if not indices or not deps.nli.available:
        return None
    pairs: list[tuple[str, str]] = []
    for n in indices:
        s = judge.rationale[n]
        hyp = nli_hypothesis(s.text)
        if judge.extractive:  # 'Source: "quote"' -> test the quoted evidence span itself
            hyp = " ".join(_QUOTED.findall(s.text)) or hyp
        pairs.append((nli_premise(s.evidence_ids, by_id), hyp))
    try:
        scores = await asyncio.wait_for(deps.nli.score(pairs), NLI_TIMEOUT_S)
    except MLUnavailable as exc:
        log.warning("verifier_nli_unavailable", error=str(exc)[:200])
        return None
    except TimeoutError:
        # CPU / memory contention (live: 4 pairs took 85 s while the weights were paged out). The
        # local key then uses the deterministic coverage check alone instead of blocking the job.
        log.warning("verifier_nli_timeout", timeout_s=NLI_TIMEOUT_S, pairs=len(pairs))
        return NLI_TIMED_OUT
    return {
        n: _NLIScore(float(sc.entailment), float(sc.contradiction))
        for n, sc in zip(indices, scores, strict=True)
    }


def _local_key(
    sentence: str, ids: Sequence[str], by_id: dict[str, EvidenceItem], score: _NLIScore, thr: float
) -> tuple[bool, str]:
    if score.entailment >= thr:
        return True, ""
    low = f"NLI entailment {score.entailment:.2f} < {thr:.2f}"
    if score.contradiction >= NLI_CONTRADICTION_VETO:
        return False, f"NLI: cited text contradicts the sentence ({score.contradiction:.2f})"
    coverage, missing_numbers = lexical_coverage(sentence, ids, by_id)
    if missing_numbers:
        return False, f"{low} and number(s) {missing_numbers} not in the cited text"
    if coverage < LEXICAL_COVERAGE_MIN:
        return False, f"{low} and only {coverage:.0%} of the words are in the cited text"
    return True, (
        f"{low} (headline-style premise); passed: no contradiction ({score.contradiction:.2f}) "
        f"and {coverage:.0%} lexical coverage"
    )


def _lexical_key(sentence: str, ids: Sequence[str], by_id: dict[str, EvidenceItem]) -> tuple[bool, str]:
    """Local key when NLI timed out: the deterministic coverage check alone (numbers + content words)."""
    coverage, missing_numbers = lexical_coverage(sentence, ids, by_id)
    if missing_numbers:
        return False, f"NLI timed out; number(s) {missing_numbers} not in the cited text"
    if coverage < LEXICAL_COVERAGE_MIN:
        return False, f"NLI timed out; only {coverage:.0%} of the words are in the cited text"
    return True, f"NLI timed out ({NLI_TIMEOUT_S:g}s); passed on {coverage:.0%} lexical coverage"


async def verify(judge: JudgeOutput, items: Sequence[EvidenceItem], deps: Deps) -> VerifierOutput:
    """Check every rationale sentence of `judge` against this job's items. Never raises."""
    by_id = {i.id: i for i in items}
    job_urls = {i.url for i in items}
    if not judge.rationale:
        return VerifierOutput(
            ok=False, checks=[], failing_sentences=["(empty rationale)"], methods=["deterministic"]
        )
    det: dict[int, tuple[bool, str]] = {
        n: deterministic_check(s.text, s.evidence_ids, by_id, job_urls) for n, s in enumerate(judge.rationale)
    }
    passed = [n for n, (ok, _) in det.items() if ok]
    methods = ["deterministic"]

    async def no_llm() -> tuple[dict[int, tuple[bool, str]], str | None]:
        return {}, None

    # The LLM key and the local NLI key are independent: run them concurrently.
    (llm_res, llm_error), nli = await asyncio.gather(
        no_llm() if judge.extractive else _llm_support(deps, judge, passed, by_id),
        _nli(deps, judge, passed, by_id),
    )
    methods.append("exact_quote" if judge.extractive else "llm")
    nli_timed_out = nli is NLI_TIMED_OUT
    if nli_timed_out:
        methods += ["nli_timeout", "lexical"]
        nli = None
    elif nli is not None:
        methods.append("nli")
    thr = deps.settings.nli_entail_min
    checks: list[GroundingCheck] = []
    for n, s in enumerate(judge.rationale):
        det_ok, reason = det[n]
        llm_supported: bool | None = None
        ok = det_ok
        score = nli.get(n) if nli is not None and det_ok else None
        if det_ok:
            if judge.extractive:
                if not quote_check(s.text, s.evidence_ids, by_id):
                    ok, reason = False, "quoted text is not an exact span of the cited item"
            elif n in llm_res:
                llm_supported, why = llm_res[n]
                if not llm_supported:
                    ok, reason = False, f"LLM verifier: not supported ({why})"
            else:
                ok = False
                reason = (
                    f"LLM verifier unavailable ({llm_error})" if llm_error else "LLM verifier gave no verdict"
                )
            if ok and nli_timed_out:
                ok, reason = _lexical_key(s.text, s.evidence_ids, by_id)
            elif ok and score is not None:
                ok, reason = _local_key(s.text, s.evidence_ids, by_id, score, thr)
                if (
                    "coverage" in reason or "words" in reason or "number" in reason
                ) and "lexical" not in methods:
                    methods.append("lexical")
        checks.append(
            GroundingCheck(
                sentence_index=n,
                text=s.text,
                evidence_ids=list(s.evidence_ids),
                deterministic_ok=det_ok,
                llm_supported=llm_supported,
                nli_entailment=round(score.entailment, 4) if score is not None else None,
                ok=ok,
                reason=reason,
            )
        )
    failing = [c.text for c in checks if not c.ok]
    log.info(
        "verifier_done",
        sentences=len(checks),
        failing=len(failing),
        methods=methods,
        reasons=[c.reason for c in checks if c.reason][:5],
    )
    return VerifierOutput(ok=not failing, checks=checks, failing_sentences=failing, methods=methods)
