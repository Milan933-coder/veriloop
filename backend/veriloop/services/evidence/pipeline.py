"""gather_evidence / followup_search: the evidence branch (BUILD_SPEC §5, SPEC §3, D-15, D-16).

Flow (all tool calls through ToolClient; per-tool timeouts; never raises to the orchestrator):
  1. sources in parallel: ClaimReview feed (+ Fact Check API when keyed), Google News (claim + "fact
     check" variant, + original language for non-English claims, E9), wire (site: filters), Bing News,
     PIB (Indian-government claims only), GDELT (corroboration only), Wikipedia, Wikidata, Serper (keyed)
  2. merge + dedupe, fact-check detection, headline verdicts, embeddings, relevance ranking
  3. in parallel: one batched stance call on the top items | Google News link decoding | up to 4
     fact-check pages (schema.org ClaimReview JSON-LD)
  4. independence groups, fast-path flag, URL validation, stable ids E1..En, persistence
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog
from rapidfuzz import fuzz

from veriloop.contracts import (
    BranchResult,
    BranchStatus,
    ClaimExtraction,
    EvidenceItem,
    ResolvedInput,
    RetrievalMethod,
    Stance,
    ToolError,
    utcnow,
)
from veriloop.core.errors import MLUnavailable
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.evidence import claimreview as cr
from veriloop.services.evidence import sources as src
from veriloop.services.evidence import stance as st
from veriloop.services.evidence.grouping import assign_groups, collapse_near_duplicates
from veriloop.services.evidence.lists import factchecker_for, looks_like_fact_check_title
from veriloop.services.evidence.models import Candidate, SourceOutcome
from veriloop.services.evidence.text import (
    QueryPlan,
    keywords,
    norm_title,
    plan_queries,
    relevance,
)
from veriloop.services.evidence.wikidata import ground_person

log = structlog.get_logger(__name__)

STANCE_K = 12  # items sent to the stance call (plus fact-checks, capped at STANCE_MAX)
STANCE_MAX = 16
MAX_ITEMS = 24  # items kept in the branch result
DECODE_K = 10  # Google News links decoded to publisher URLs
FACTCHECK_PAGES = 4  # fact-check pages fetched for JSON-LD per job
RELEVANCE_MIN = 0.30
SOURCE_DEADLINE_S = 6.0
ENRICH_DEADLINE_S = 9.0  # stance LLM batches (5.5 s cap) + local fallback, decode, fact-check pages
PREFILTER_K = 60  # candidates embedded for ranking (CPU cost grows linearly with this)
OPTIONAL_SOURCES = frozenset({"gdelt"})  # never awaited past the required sources (research.md)
KEY_TOOLS = frozenset({"gnews_rss", "bing_news_rss", "wire_rss", "claimreview_feed", "factcheck", "serper"})
_background: set[asyncio.Task[Any]] = set()  # slow optional calls allowed to finish and fill the cache


SECONDARY_DEADLINE_S = 3.0


@dataclass
class _Run:
    errors: list[ToolError] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tools_used: dict[str, str] = field(default_factory=dict)
    allowed_urls: set[str] = field(default_factory=set)
    timings: dict[str, int] = field(default_factory=dict)
    # job name -> (query, locale) for the search jobs, so a failed primary can be re-run on its
    # secondary tool (config/tools.yaml `secondary`, resolved by ToolClient.secondary_for).
    queries: dict[str, tuple[str, str]] = field(default_factory=dict)

    def absorb(self, deps: Deps, outcome: SourceOutcome) -> list[Candidate]:
        self.tools_used[outcome.tool] = deps.tools.tool_versions().get(outcome.tool, "")
        if outcome.error is not None:
            if outcome.error.error_type == "unconfigured":
                self.notes.append(f"{outcome.tool}: unconfigured ({outcome.error.message})")
            else:
                self.errors.append(outcome.error)
        if outcome.note:
            self.notes.append(outcome.note)
        self.allowed_urls |= outcome.returned_urls
        return outcome.candidates


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


async def _run_parallel(
    jobs: dict[str, Coroutine[Any, Any, Any]],
    deadline_s: float,
    *,
    keep_running: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], list[str]]:
    """Run coroutines concurrently; return results of those done by the deadline + names that timed out.

    Names in `keep_running` are optional (GDELT: corroboration only, 10-20 s when it answers): the
    wait ends as soon as every REQUIRED task is done (or the deadline passes); an optional task is
    collected only if it already finished, otherwise it keeps running in the background so the
    ToolClient cache is warm next time. Every other straggler is cancelled.
    """
    tasks = {name: asyncio.ensure_future(coro) for name, coro in jobs.items()}
    if not tasks:
        return {}, []
    required = [t for name, t in tasks.items() if name not in keep_running]
    await asyncio.wait(required or list(tasks.values()), timeout=deadline_s)
    results: dict[str, Any] = {}
    late: list[str] = []
    for name, task in tasks.items():
        if task.done():
            exc = task.exception()
            if exc is not None:
                log.warning("evidence_task_failed", task=name, error=f"{type(exc).__name__}: {exc}")
                results[name] = exc
            else:
                results[name] = task.result()
        else:
            late.append(name)
            if name in keep_running:
                _background.add(task)
                task.add_done_callback(_background.discard)
            else:
                task.cancel()
    return results, late


def _english_locale(locale: str) -> str:
    country = (locale.split("-")[1] if "-" in (locale or "") else "IN") or "IN"
    return f"en-{country}"


async def _embed(deps: Deps, texts: list[str], kind: str) -> np.ndarray | None:
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)
    try:
        return await deps.embedder.embed(texts, kind=kind)  # type: ignore[arg-type]
    except MLUnavailable:
        return None


def _source_jobs(
    deps: Deps, plan: QueryPlan, qvecs: np.ndarray | None, pvecs: np.ndarray | None, run: _Run
) -> dict[str, Coroutine[Any, Any, Any]]:
    en = _english_locale(plan.locale)
    jobs: dict[str, Coroutine[Any, Any, Any]] = {
        "claimreview_feed": cr.search_feed(deps, [plan.claim_en, plan.claim], qvecs, pvecs),
        "gnews_main": src.search_gnews(deps, plan.main, en),
        "gnews_factcheck": src.search_gnews(deps, plan.fact_check, en, op="search_factcheck"),
        "wire": src.search_wire(deps, " ".join(keywords(plan.claim_en, limit=8)), en),
        "bing_main": src.search_bing(deps, plan.main, en),
        "bing_factcheck": src.search_bing(deps, plan.fact_check, en),
        "gdelt": src.search_gdelt(deps, src.entity_terms(plan.entities, plan.claim_en), timeout_s=12.0),
        "wikipedia": src.search_wikipedia(
            deps, " ".join(plan.entities) or " ".join(keywords(plan.claim_en, limit=5))
        ),
    }
    if deps.tools.is_configured("factcheck"):
        jobs["factcheck"] = cr.search_factcheck_api(deps, plan.claim_en, "en")
        if plan.native:
            jobs["factcheck_native"] = cr.search_factcheck_api(deps, plan.claim, plan.language)
    else:
        run.notes.append(
            "factcheck: unconfigured (GOOGLE_FACTCHECK_API_KEY not set); keyless ClaimReview feed used"
        )
    if deps.tools.is_configured("serper"):
        jobs["serper_news"] = src.search_serper(deps, plan.main, en, kind="news")
        jobs["serper_web"] = src.search_serper(deps, plan.main, en, kind="search")
    else:
        run.notes.append("serper: unconfigured (SERPER_API_KEY not set); Google News + Bing + Wikipedia used")
    if plan.native and plan.native_locale:
        jobs["gnews_native"] = src.search_gnews(deps, plan.native, plan.native_locale, op="search_native")
        jobs["gnews_native_factcheck"] = src.search_gnews(
            deps, plan.native_fact_check or plan.native, plan.native_locale, op="search_native_factcheck"
        )
        jobs["bing_native"] = src.search_bing(deps, plan.native, plan.native_locale)
    if plan.government:
        jobs["pib_rss"] = src.fetch_pib(deps)
        jobs["gnews_pib"] = src.search_gnews(
            deps,
            " ".join(keywords(plan.claim_en, limit=6)) + " site:pib.gov.in",
            en,
            op="search_pib",
            limit=15,
        )
    for i, person in enumerate(plan.person_entities[:2]):
        jobs[f"wikidata_{i}"] = ground_person(deps, person, plan.claim_en, plan.claim_type)
    run.queries.update(
        {
            "gnews_main": (plan.main, en),
            "gnews_factcheck": (plan.fact_check, en),
            "wire": (" ".join(keywords(plan.claim_en, limit=8)), en),
            "bing_main": (plan.main, en),
            "bing_factcheck": (plan.fact_check, en),
            "serper_news": (plan.main, en),
            "serper_web": (plan.main, en),
        }
    )
    if plan.native and plan.native_locale:
        run.queries["gnews_native"] = (plan.native, plan.native_locale)
        run.queries["bing_native"] = (plan.native, plan.native_locale)
    return jobs


def _secondary_jobs(
    deps: Deps, failed: list[str], run: _Run, ran_ok: set[str]
) -> dict[str, Coroutine[Any, Any, Any]]:
    """Re-run failed primary searches on their configured secondary (ToolClient.secondary_for).

    Identical (tool, query) pairs that already ran hit the ToolClient cache, so this is cheap when
    the secondary was part of the first round anyway; it matters when it was not (e.g. wire_rss ->
    bing_news_rss with the wire query, serper -> Google News).
    """
    jobs: dict[str, Coroutine[Any, Any, Any]] = {}
    for name in failed:
        tool = _tool_for(name)
        query = run.queries.get(name)
        if query is None:
            continue
        for sec in deps.tools.secondary_for(tool):
            if sec == "bing_news_rss":
                job = f"secondary_bing:{name}"
                if job not in jobs and f"bing:{query}" not in ran_ok:
                    jobs[job] = src.search_bing(deps, query[0], query[1])
            elif sec == "gnews_rss":
                job = f"secondary_gnews:{name}"
                if job not in jobs and f"gnews:{query}" not in ran_ok:
                    jobs[job] = src.search_gnews(deps, query[0], query[1], op="search_secondary")
            else:
                continue  # claimreview_feed always runs in the first round; page_fetch is not a search
            run.notes.append(f"{tool} failed for {name}; secondary {sec} used")
            break
    return jobs


def _dedupe(cands: list[Candidate]) -> list[Candidate]:
    """One candidate per URL and per (publisher, headline); prefer real URLs and longer snippets."""
    best: dict[tuple[str, str], Candidate] = {}
    order: list[tuple[str, str]] = []
    for c in cands:
        if c.tool in ("claimreview_feed", "factcheck", "wikidata"):
            key = ("url", c.url)
        else:
            key = (c.host.removeprefix("m."), norm_title(c.title)[:90])
        cur = best.get(key)
        if cur is None:
            best[key] = c
            order.append(key)
        elif (c.gnews_link is None, len(c.snippet)) > (cur.gnews_link is None, len(cur.snippet)):
            best[key] = c
    seen_urls: set[str] = set()
    out = []
    for key in order:
        c = best[key]
        if c.url in seen_urls:
            continue
        seen_urls.add(c.url)
        out.append(c)
    return out


def _lexical_relevance(claim: str, text: str) -> float:
    # Embedder unavailable: token-set overlap is a crude stand-in, deliberately conservative.
    return max(0.0, min(1.0, (fuzz.token_set_ratio(claim.lower(), text.lower()) / 100 - 0.4) / 0.5))


async def _score(
    deps: Deps,
    plan: QueryPlan,
    qvecs: np.ndarray | None,
    pvecs: np.ndarray | None,
    cands: list[Candidate],
    run: _Run,
) -> None:
    """Relevance (query-passage scale) for every candidate; claim similarity (both scales) for reviews."""
    passages = [c.title if c.snippet == c.title else f"{c.title}. {c.snippet}"[:500] for c in cands]
    vecs = await _embed(deps, passages, "passage") if qvecs is not None else None
    for i, c in enumerate(cands):
        if vecs is not None and qvecs is not None:
            c.embedding = vecs[i]
            cos = float(np.max(qvecs @ vecs[i]))
            if c.tool == "claimreview_feed" and c.claimreview is not None:
                c.relevance = c.claimreview.similarity
            else:
                c.relevance = round(relevance(cos), 4)
            if c.claimreview is not None and c.tool != "claimreview_feed":
                c.claimreview.similarity = cr.match_similarity(vecs[i], qvecs, pvecs)
        else:
            c.relevance = round(_lexical_relevance(plan.claim_en, passages[i]), 4)
    if vecs is None:
        run.notes.append("relevance: embedder unavailable, lexical overlap used")


def _mark_fact_checks(cands: list[Candidate]) -> None:
    for c in cands:
        if c.tool in ("claimreview_feed", "factcheck", "wikidata", "wikipedia"):
            continue
        checker = factchecker_for(c.host, url=c.url if c.gnews_link is None else "", title=c.title)
        if checker is not None or (c.is_government and looks_like_fact_check_title(c.title)):
            c.is_fact_check = True
            cr.apply_headline_rating(c)


def _prefilter(cands: list[Candidate], plan: QueryPlan) -> list[Candidate]:
    """Cheap lexical cut before embedding: keep structured / fact-check / government items plus the
    candidates whose headline overlaps the claim most (rapidfuzz, no model), PREFILTER_K in total."""
    if len(cands) <= PREFILTER_K:
        return cands
    queries = [q.lower() for q in dict.fromkeys([plan.claim_en, plan.claim]) if q]
    keep = [
        c
        for c in cands
        if c.pinned
        or c.is_fact_check
        or c.is_government
        or c.tool in ("claimreview_feed", "factcheck", "wikidata")
    ]
    kept = {id(c) for c in keep}
    rest = [c for c in cands if id(c) not in kept]
    rest.sort(key=lambda c: -max(fuzz.token_set_ratio(q, c.title.lower()) for q in queries))
    return keep + rest[: max(0, PREFILTER_K - len(keep))]


def _select(cands: list[Candidate]) -> list[Candidate]:
    pinned = [
        c
        for c in cands
        if c.tool == "wikidata"
        or (c.tool in ("claimreview_feed", "factcheck") and c.claimreview and c.claimreview.similarity >= 0.6)
    ]
    pinned_ids = {id(c) for c in pinned}  # identity, not dataclass equality (fields hold numpy arrays)
    others = [c for c in cands if id(c) not in pinned_ids and c.relevance >= RELEVANCE_MIN]
    pinned.sort(
        key=lambda c: (c.tool == "wikidata", -(c.claimreview.similarity if c.claimreview else 0), c.url)
    )
    others.sort(key=lambda c: (-c.relevance, -int(c.is_fact_check), c.url))
    selected = pinned + others[: max(0, MAX_ITEMS - len(pinned))]
    for n, c in enumerate(selected, 1):
        c.item_id = f"E{n}"
    return selected


def _stance_set(selected: list[Candidate]) -> list[Candidate]:
    pool = [c for c in selected if c.tool != "wikidata"]
    top = pool[:STANCE_K]
    extra = [c for c in pool[STANCE_K:] if c.is_fact_check or c.claimreview is not None]
    return (top + extra)[:STANCE_MAX]


class _Decoder:
    """Decode each Google News link at most once per job (decode + fact-check fetch share it)."""

    def __init__(self, deps: Deps, run: _Run) -> None:
        self.deps, self.run = deps, run
        self._tasks: dict[str, asyncio.Task[tuple[str | None, ToolError | None]]] = {}
        self._sem = asyncio.Semaphore(4)

    async def _one(self, link: str) -> tuple[str | None, ToolError | None]:
        async with self._sem:
            return await src.decode_gnews(self.deps, link)

    async def resolve(self, c: Candidate) -> bool:
        if not c.gnews_link:
            return True
        if c.url != c.gnews_link:
            return True
        task = self._tasks.get(c.gnews_link)
        if task is None:
            task = self._tasks[c.gnews_link] = asyncio.ensure_future(self._one(c.gnews_link))
        url, err = await task
        if url:
            c.url = url
            self.run.allowed_urls.add(url)
            return True
        if err is not None and err not in self.run.errors:
            self.run.errors.append(err)
        return False


async def _decode(decoder: _Decoder, cands: list[Candidate]) -> None:
    await asyncio.gather(*(decoder.resolve(c) for c in cands))


async def _factcheck_pages(
    deps: Deps,
    decoder: _Decoder,
    cands: list[Candidate],
    qvecs: np.ndarray | None,
    pvecs: np.ndarray | None,
    run: _Run,
) -> None:
    targets = [c for c in cands if c.is_fact_check and c.tool not in ("claimreview_feed", "factcheck")][
        :FACTCHECK_PAGES
    ]

    async def one(c: Candidate) -> None:
        if not await decoder.resolve(c):
            return
        page, err = await cr.fetch_factcheck_page(deps, c.url)
        if err is not None:
            run.errors.append(err)
            return
        if cr.apply_page_rating(c, page) and c.claimreview is not None:
            text = c.claimreview.claim_text  # the page's own claimReviewed: compare claim to claim
            if text and qvecs is not None:
                vec = await _embed(deps, [text[:512]], "passage")
                if vec is not None:
                    c.claimreview.similarity = cr.match_similarity(vec[0], qvecs, pvecs)
            elif c.embedding is not None and qvecs is not None:
                c.claimreview.similarity = cr.match_similarity(c.embedding, qvecs, pvecs)

    await asyncio.gather(*(one(c) for c in targets))


def _to_item(
    c: Candidate,
    versions: dict[str, str],
    retrieved_at: Any,
    branch: str = "evidence",
    method: RetrievalMethod | None = None,
) -> EvidenceItem:
    quote = c.stance_quote if c.stance_quote and c.stance_quote in c.snippet else None
    stance = c.stance if (quote or c.stance in (Stance.NEUTRAL, Stance.UNKNOWN)) else Stance.NEUTRAL
    return EvidenceItem(
        id=c.item_id,
        source_name=c.source_name or c.domain,
        url=c.url,
        domain=c.domain,
        publisher_group=c.group or c.domain,
        title=c.title or None,
        snippet=c.snippet,
        published_at=c.published_at,
        retrieved_at=retrieved_at,
        language=c.language,
        confidence_relevance=round(max(0.0, min(1.0, c.relevance)), 4),
        stance=stance,
        stance_confidence=round(c.stance_confidence, 4) if stance is not Stance.UNKNOWN else 0.0,
        stance_quote=quote if stance in (Stance.SUPPORT, Stance.REFUTE) else None,
        retrieval_method=method or c.retrieval_method,
        tool=c.tool,
        tool_version=versions.get(c.tool, ""),
        branch=branch,
        is_fact_check=c.is_fact_check,
        is_government_source=c.is_government,
        claimreview=c.claimreview,
    )


def validate_urls(items: list[EvidenceItem], allowed: set[str]) -> tuple[list[EvidenceItem], int]:
    """No URL may appear that a tool did not return in this job (SPEC §3: never synthesize a URL)."""
    kept = [i for i in items if i.url in allowed]
    return kept, len(items) - len(kept)


def _fast_path(items: list[EvidenceItem]) -> bool:
    from veriloop.services.fusion.config import load_fusion_config
    from veriloop.services.fusion.signals import claimreview_matches, fast_path

    cfg = load_fusion_config()
    return fast_path(claimreview_matches(items, cfg), cfg)


def _job_id(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    ctx = structlog.contextvars.get_contextvars()
    value = ctx.get("job_id")
    return str(value) if value else None


async def _persist(
    deps: Deps, job_id: str | None, items: list[EvidenceItem], vecs: list[Any], run: _Run
) -> None:
    if not job_id or not items:
        return
    try:
        embeddings = vecs if all(v is not None for v in vecs) else None
        await repo.insert_evidence_items(deps.db, job_id, items, embeddings)
    except Exception as exc:  # e.g. the job row does not exist yet (FK); evidence is still returned
        run.notes.append(f"persist: evidence items not stored ({type(exc).__name__})")
        log.warning("evidence_persist_failed", job_id=job_id, error=str(exc)[:300])


def _status(items: list[EvidenceItem], errors: list[ToolError]) -> BranchStatus:
    if not items:
        return BranchStatus.INSUFFICIENT
    if any(e.tool in KEY_TOOLS and e.op != "decode" for e in errors):
        return BranchStatus.DEGRADED
    return BranchStatus.OK


async def gather_evidence(
    extraction: ClaimExtraction,
    resolved: ResolvedInput | None,
    locale: str,
    deps: Deps,
    *,
    job_id: str | None = None,
) -> BranchResult:
    """BUILD_SPEC §5. Never raises: failures become ToolErrors and a branch status."""
    t0 = time.perf_counter()
    try:
        return await _gather(extraction, resolved, locale, deps, job_id=_job_id(job_id), t0=t0)
    except Exception as exc:
        log.exception("evidence_branch_failed")
        return BranchResult(
            branch="evidence",
            status=BranchStatus.FAILED,
            errors=[
                ToolError(
                    tool="node:evidence",
                    op="gather",
                    error_type="exception",
                    message=f"{type(exc).__name__}: {exc}"[:500],
                )
            ],
            latency_ms=_ms(t0),
        )


async def _gather(
    extraction: ClaimExtraction,
    resolved: ResolvedInput | None,
    locale: str,
    deps: Deps,
    *,
    job_id: str | None,
    t0: float,
) -> BranchResult:
    plan = plan_queries(extraction, resolved, locale)
    if plan is None:
        return BranchResult(
            branch="evidence", status=BranchStatus.INSUFFICIENT, notes=["no claim to search for"]
        )
    run = _Run()

    t = time.perf_counter()
    query_texts = list(dict.fromkeys([plan.claim_en, plan.claim]))
    # Both e5 prefixes: "query" for retrieval relevance, "passage" for claim-to-claim similarity.
    qvecs, pvecs = await asyncio.gather(
        _embed(deps, query_texts, "query"), _embed(deps, query_texts, "passage")
    )
    run.timings["embed_query"] = _ms(t)

    t = time.perf_counter()
    jobs = _source_jobs(deps, plan, qvecs, pvecs, run)
    results, late = await _run_parallel(jobs, SOURCE_DEADLINE_S, keep_running=OPTIONAL_SOURCES)
    run.timings["sources"] = _ms(t)
    cands: list[Candidate] = []
    failed: list[str] = list(late)
    ran_ok: set[str] = set()
    for name in jobs:
        res = results.get(name)
        if isinstance(res, SourceOutcome):
            cands.extend(run.absorb(deps, res))
            if res.error is not None and res.error.error_type != "unconfigured":
                failed.append(name)
            elif res.error is None and name in run.queries:
                kind = "bing" if name.startswith("bing") else "gnews" if name.startswith("gnews") else name
                ran_ok.add(f"{kind}:{run.queries[name]}")
        elif isinstance(res, BaseException):
            failed.append(name)
            run.errors.append(
                ToolError(tool=_tool_for(name), op=name, error_type="exception", message=str(res)[:300])
            )
    secondary = _secondary_jobs(deps, failed, run, ran_ok)
    if secondary:
        t2 = time.perf_counter()
        sec_results, _sec_late = await _run_parallel(secondary, SECONDARY_DEADLINE_S)
        for res in sec_results.values():
            if isinstance(res, SourceOutcome):
                cands.extend(run.absorb(deps, res))
        run.timings["secondary"] = _ms(t2)
    for name in late:
        run.tools_used[_tool_for(name)] = deps.tools.tool_versions().get(_tool_for(name), "")
        if name in OPTIONAL_SOURCES:
            # Not a failure: we chose not to wait. It finishes in the background and fills the cache.
            run.notes.append(f"{name}: not awaited (corroboration only); still running, cached for later")
            continue
        run.errors.append(
            ToolError(
                tool=_tool_for(name),
                op=name,
                error_type="timeout",
                message=f"no result within the {SOURCE_DEADLINE_S:g}s evidence deadline",
            )
        )

    t = time.perf_counter()
    cands = _dedupe(cands)
    _mark_fact_checks(cands)
    n_candidates = len(cands)
    cands = _prefilter(cands, plan)
    await _score(deps, plan, qvecs, pvecs, cands, run)
    selected = _select(cands)
    run.timings["rank"] = _ms(t)

    t = time.perf_counter()
    stance_claim = plan.claim if plan.claim == plan.claim_en else f"{plan.claim} (English: {plan.claim_en})"
    decode_targets = [c for c in selected if c.gnews_link][:DECODE_K]
    decoder = _Decoder(deps, run)
    enrich: dict[str, Coroutine[Any, Any, Any]] = {
        "stance": st.classify(deps, stance_claim, _stance_set(selected)),
        "decode": _decode(decoder, decode_targets),
        "factcheck_pages": _factcheck_pages(deps, decoder, selected, qvecs, pvecs, run),
    }
    enriched, late2 = await _run_parallel(enrich, ENRICH_DEADLINE_S)
    run.timings["enrich"] = _ms(t)
    stance_res = enriched.get("stance")
    if isinstance(stance_res, tuple):
        run.notes.extend(stance_res[0])
        run.notes.append(f"stance method: {stance_res[1]}")
    for name in late2:
        run.errors.append(
            ToolError(
                tool="llm_primary"
                if name == "stance"
                else "page_fetch"
                if name == "factcheck_pages"
                else "gnews_rss",
                op=name,
                error_type="timeout",
                message=f"{name} exceeded {ENRICH_DEADLINE_S:g}s",
            )
        )

    assign_groups(selected)
    merges = collapse_near_duplicates(selected)
    if merges:
        run.notes.append(f"grouping: {merges} near-duplicate headline merge(s)")

    versions = deps.tools.tool_versions()
    now = utcnow()
    items = [_to_item(c, versions, now) for c in selected]
    items, dropped = validate_urls(items, run.allowed_urls)
    if dropped:
        run.notes.append(f"url_validator: dropped {dropped} item(s) with URLs no tool returned")
    vec_by_id = {c.item_id: c.embedding for c in selected}
    await _persist(deps, job_id, items, [vec_by_id.get(i.id) for i in items], run)

    fast = _fast_path(items)
    groups = {i.publisher_group for i in items if i.tool != "wikidata"}
    run.timings["total"] = _ms(t0)
    run.notes.append("timing_ms: " + ", ".join(f"{k}={v}" for k, v in run.timings.items()))
    status = _status(items, run.errors)
    log.info(
        "evidence_done",
        status=status.value,
        items=len(items),
        groups=len(groups),
        candidates=n_candidates,
        embedded=len(cands),
        errors=[f"{e.tool}:{e.error_type}" for e in run.errors],
        fast_path=fast,
        **{f"ms_{k}": v for k, v in run.timings.items()},
    )
    return BranchResult(
        branch="evidence",
        status=status,
        items=items,
        errors=run.errors,
        tools_used=run.tools_used,
        latency_ms=_ms(t0),
        notes=run.notes,
        fast_path_hit=fast,
    )


_TOOL_BY_JOB = {
    "claimreview_feed": "claimreview_feed",
    "factcheck": "factcheck",
    "factcheck_native": "factcheck",
    "wire": "wire_rss",
    "gdelt": "gdelt",
    "wikipedia": "wikipedia",
    "pib_rss": "pib_rss",
    "serper_news": "serper",
    "serper_web": "serper",
}


def _tool_for(job_name: str) -> str:
    job_name = job_name.split(":", 1)[0].removeprefix("secondary_")
    if job_name in _TOOL_BY_JOB:
        return _TOOL_BY_JOB[job_name]
    if job_name.startswith("gnews"):
        return "gnews_rss"
    if job_name.startswith("bing"):
        return "bing_news_rss"
    if job_name.startswith("wikidata"):
        return "wikidata"
    return job_name


# ---- debate follow-up ---------------------------------------------------------------------------
FOLLOWUP_K = 5
FOLLOWUP_DEADLINE_S = 4.0


async def followup_search(query: str, deps: Deps, *, start_index: int = 1) -> list[EvidenceItem]:
    """One debate follow-up search (news + Bing + ClaimReview feed). Ids D{start_index}..; never raises."""
    query = " ".join((query or "").split())[:300]
    if not query:
        return []
    try:
        return await _followup(query, deps, start_index)
    except Exception as exc:
        log.warning("followup_search_failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return []


async def _followup(query: str, deps: Deps, start_index: int) -> list[EvidenceItem]:
    run = _Run()
    qvecs, pvecs = await asyncio.gather(_embed(deps, [query], "query"), _embed(deps, [query], "passage"))
    jobs: dict[str, Coroutine[Any, Any, Any]] = {
        "gnews": src.search_gnews(deps, query, "en-IN", op="followup", limit=20),
        "bing": src.search_bing(deps, query, "en-IN"),
        "claimreview_feed": cr.search_feed(deps, [query], qvecs, pvecs, limit=3),
    }
    results, _late = await _run_parallel(jobs, FOLLOWUP_DEADLINE_S)
    cands: list[Candidate] = []
    for res in results.values():
        if isinstance(res, SourceOutcome):
            cands.extend(run.absorb(deps, res))
    cands = _dedupe(cands)
    _mark_fact_checks(cands)
    plan = QueryPlan(claim=query, claim_en=query, language="en", locale="en-IN", main=query, fact_check=query)
    await _score(deps, plan, qvecs, pvecs, cands, run)
    cands = [c for c in cands if c.relevance >= RELEVANCE_MIN or c.tool == "claimreview_feed"]
    cands.sort(key=lambda c: (-c.relevance, c.url))
    picked = cands[:FOLLOWUP_K]
    assign_groups(picked)
    for n, c in enumerate(picked):
        c.item_id = f"D{start_index + n}"
        c.stance, c.stance_quote, c.stance_confidence = Stance.UNKNOWN, None, 0.0
    versions = deps.tools.tool_versions()
    now = utcnow()
    items = [
        _to_item(c, versions, now, branch="debate", method=RetrievalMethod.FOLLOWUP_SEARCH) for c in picked
    ]
    items, _ = validate_urls(items, run.allowed_urls)
    log.info("followup_search_done", query=query[:120], items=len(items), errors=len(run.errors))
    return items
