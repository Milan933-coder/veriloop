"""Stance of each evidence item towards the claim (one batched LLM call; NLI / lexicon fallback).

Guardrails (D-16, G1):
* the prompt forbids outside knowledge: stance is read from the item text only;
* every non-neutral stance needs an EXACT quote that is a substring of the item's snippet. A quote
  that is not found (after whitespace/quote-mark normalisation, mapped back to the exact snippet
  span) turns the stance neutral;
* hedged or question headlines ("may", "could", "will it be", "हो सकता है", "...?") are capped at
  neutral unless the item is a fact-check with a verdict word (research.md demo case (d)).
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from veriloop.contracts import Stance
from veriloop.core.errors import LLMDisabled, MLUnavailable, ToolCallFailed
from veriloop.deps import Deps
from veriloop.services.evidence.lexicon import detect_verdict, is_hedged
from veriloop.services.evidence.models import Candidate

log = structlog.get_logger(__name__)

NLI_DECISIVE = 0.60
LLM_BATCH = 8  # items per stance call; batches run in parallel
LLM_TIMEOUT_S = 5.5  # per batch; a late batch falls back to local NLI / lexicon
STANCE_TEXT_CHARS = 450  # prompt size drives latency; quotes are still located in the full snippet


class StanceJudgement(BaseModel):
    id: str = Field(description="the item id exactly as given")
    stance: Literal["support", "refute", "neutral"]
    confidence: float = Field(description="0..1")
    quote: str = Field(
        description="exact span copied from the item's TEXT that justifies the stance; '' if neutral"
    )


class StanceBatch(BaseModel):
    items: list[StanceJudgement]


SYSTEM = """You classify the stance of news/fact-check snippets toward ONE claim, for a fact-checking system.
Rules:
- Use ONLY the item TEXT given. Never use your own knowledge, memory or assumptions about events; if the
  text alone does not settle it, answer "neutral".
- "support": the text asserts the claim's specific facts are true (same event, same numbers, same actors).
- "refute": the text says the claim is false, fake, misleading, edited/doctored/AI-generated/deepfake,
  out of context or an old/unrelated video, OR reports facts that directly contradict it.
  A fact-check that rates THIS claim false/misleading/altered is "refute".
- "neutral": a different event (e.g. a state-level decision when the claim is about the central government,
  a different date or a different number), speculation or projections ("may", "could", "expected", "likely",
  "will it be", calculators, what-if tables), questions, opinion, or text that only mentions the topic.
- Also "neutral": text that only relays what ONE involved party asserts about a contested event
  ("Iran says it shot down...", "X claims...", "according to state media...") without confirming it.
  Reporting that someone claims something is not confirmation. (An official body announcing its own
  decision, e.g. "Cabinet approves 3% DA hike", does state the fact and can be "support".)
- For a "Claim reviewed: ... — Rating: ..." item: if the reviewed claim is the same as (or equivalent to) our
  claim, its rating decides (False/Misleading/Altered -> refute, True -> support); if it is a different
  claim, answer neutral.
- quote: copy the shortest span of the item TEXT, character for character, that justifies a support/refute
  answer. Do not paraphrase, translate or add ellipses. Use "" for neutral.
- confidence: how clearly the text settles the stance (0..1).
Return one entry per item id."""


def _norm(s: str) -> str:
    s = (
        s.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    return " ".join(s.split()).lower()


def locate_quote(quote: str, snippet: str) -> str | None:
    """Return the exact substring of `snippet` matching `quote` (whitespace/quote-mark/case tolerant)."""
    if not quote or not snippet:
        return None
    if quote in snippet:
        return quote
    q = _norm(quote).strip(" .\"'")
    if len(q) < 4:
        return None
    # Build a normalised copy of the snippet with an index map back to the original characters.
    chars: list[str] = []
    index: list[int] = []
    prev_space = False
    for i, ch in enumerate(snippet):
        c = _norm(ch) if not ch.isspace() else " "
        if c == " ":
            if prev_space or not chars:
                continue
            prev_space = True
        else:
            prev_space = False
        for cc in c or " ":
            chars.append(cc)
            index.append(i)
    hay = "".join(chars)
    pos = hay.find(q)
    if pos < 0:
        return None
    start, end = index[pos], index[pos + len(q) - 1] + 1
    return snippet[start:end]


def apply_caps(cand: Candidate) -> None:
    """Hedge cap: speculation / question headlines never count as support or refutation."""
    if cand.stance not in (Stance.SUPPORT, Stance.REFUTE):
        return
    verdict = (cand.is_fact_check or cand.claimreview is not None) and (
        detect_verdict(cand.title) or detect_verdict(cand.stance_quote or "")
    )
    if verdict:
        return
    if is_hedged(cand.stance_quote or "") or is_hedged(cand.title):
        cand.stance, cand.stance_confidence = Stance.NEUTRAL, round(cand.stance_confidence, 4)
        cand.stance_quote = None


def _render(cands: list[Candidate]) -> str:
    lines = []
    for c in cands:
        kind = "fact-check" if (c.is_fact_check or c.claimreview) else "news"
        text = c.snippet[:STANCE_TEXT_CHARS]
        lines.append(f"[{c.item_id}] ({kind}; {c.source_name})\nTITLE: {c.title}\nTEXT: {text}")
    return "\n\n".join(lines)


async def llm_stance(deps: Deps, claim: str, cands: list[Candidate]) -> list[str]:
    """Batched stance call (effort none). Returns notes; raises LLMDisabled/ToolCallFailed."""
    user = f"CLAIM: {claim}\n\nITEMS:\n{_render(cands)}"
    out = await deps.llm.complete_json(
        task="stance", system=SYSTEM, user=user, schema=StanceBatch, effort="none", max_tokens=2500
    )
    by_id = {c.item_id: c for c in cands}
    dropped = 0
    for j in out.items:
        cand = by_id.get(j.id.strip().strip("[]"))
        if cand is None:
            continue
        stance = Stance(j.stance)
        conf = max(0.0, min(1.0, float(j.confidence)))
        if stance is Stance.NEUTRAL:
            cand.stance, cand.stance_confidence, cand.stance_quote = Stance.NEUTRAL, conf, None
            continue
        exact = locate_quote(j.quote, cand.snippet)
        if exact is None:
            dropped += 1
            cand.stance, cand.stance_confidence, cand.stance_quote = Stance.NEUTRAL, 0.0, None
            continue
        cand.stance, cand.stance_confidence, cand.stance_quote = stance, conf, exact
        apply_caps(cand)
    notes = []
    if dropped:
        notes.append(f"stance: {dropped} LLM quote(s) were not exact substrings of the snippet -> neutral")
    return notes


async def nli_stance(deps: Deps, claim: str, cands: list[Candidate]) -> list[str]:
    """Extractive fallback (LLM disabled): local NLI premise=snippet, hypothesis=claim."""
    pairs = [(c.snippet[:600], claim) for c in cands]
    scores = await deps.nli.score(pairs)
    for cand, sc in zip(cands, scores, strict=True):
        if sc.entailment >= NLI_DECISIVE and sc.entailment > sc.contradiction:
            stance, conf = Stance.SUPPORT, sc.entailment
        elif sc.contradiction >= NLI_DECISIVE:
            stance, conf = Stance.REFUTE, sc.contradiction
        else:
            stance, conf = Stance.NEUTRAL, max(sc.neutral, 0.0)
        if cand.claimreview is not None and cand.claimreview.similarity >= 0.6 and stance is Stance.NEUTRAL:
            stance, conf = _rating_stance(cand)
        cand.stance, cand.stance_confidence = stance, round(float(conf), 4)
        cand.stance_quote = _first_sentence(cand) if stance is not Stance.NEUTRAL else None
        apply_caps(cand)
    return ["stance: local NLI (LLM disabled / unavailable)"]


def heuristic_stance(cands: list[Candidate]) -> list[str]:
    """No LLM and no NLI: only fact-check verdicts are decisive; everything else stays unknown."""
    for cand in cands:
        if cand.claimreview is not None and cand.claimreview.similarity >= 0.6:
            cand.stance, cand.stance_confidence = _rating_stance(cand)
            cand.stance_quote = _first_sentence(cand) if cand.stance is not Stance.NEUTRAL else None
    return ["stance: lexicon only (LLM and NLI unavailable)"]


def _rating_stance(cand: Candidate) -> tuple[Stance, float]:
    r = cand.claimreview.normalized_rating if cand.claimreview else 0.0
    if r <= -0.3:
        return Stance.REFUTE, min(1.0, abs(r))
    if r >= 0.3:
        return Stance.SUPPORT, min(1.0, r)
    return Stance.NEUTRAL, 0.0


def _first_sentence(cand: Candidate) -> str:
    m = re.match(r"(.{20,240}?[.!?।])(\s|$)", cand.snippet)
    return m.group(1) if m else cand.snippet[:240]


async def _local(deps: Deps, claim: str, cands: list[Candidate]) -> tuple[list[str], str]:
    """No (usable) LLM: local NLI when available, else fact-check ratings only (never guessed)."""
    try:
        if deps.nli.available:
            return await nli_stance(deps, claim, cands), "nli"
    except MLUnavailable:
        pass
    return heuristic_stance(cands), "lexicon"


async def classify(deps: Deps, claim: str, cands: list[Candidate]) -> tuple[list[str], str]:
    """Set stance on `cands` in place. Returns (notes, method) with method llm|nli|lexicon (or llm+...).

    The LLM sees parallel batches of LLM_BATCH items, each bounded by LLM_TIMEOUT_S: output length
    drives latency (one 16-item call measured ~5 s; two 8-item calls in parallel ~half that). A batch
    that fails or times out falls back to local NLI / lexicon for its items only, so a slow LLM never
    leaves the whole branch without stance.
    """
    if not cands:
        return [], "none"
    if not deps.llm.enabled:
        return await _local(deps, claim, cands)
    batches = [cands[i : i + LLM_BATCH] for i in range(0, len(cands), LLM_BATCH)]
    results = await asyncio.gather(
        *(asyncio.wait_for(llm_stance(deps, claim, b), LLM_TIMEOUT_S) for b in batches),
        return_exceptions=True,
    )
    notes: list[str] = []
    failed: list[Candidate] = []
    for batch, res in zip(batches, results, strict=True):
        if not isinstance(res, BaseException):
            notes.extend(res)
            continue
        if isinstance(res, ToolCallFailed):
            why = res.error.error_type
        elif isinstance(res, TimeoutError):
            why = f"timeout after {LLM_TIMEOUT_S:g}s"
        elif isinstance(res, LLMDisabled):
            why = "disabled"
        else:
            why = type(res).__name__
        log.warning("stance_llm_failed", error_type=why, items=len(batch))
        failed.extend(batch)
        notes.append(f"stance: LLM failed ({why}) for {len(batch)} item(s); fell back to local stance")
    if not failed:
        return notes, "llm"
    local_notes, local_method = await _local(deps, claim, failed)
    method = local_method if len(failed) == len(cands) else f"llm+{local_method}"
    return notes + local_notes, method
