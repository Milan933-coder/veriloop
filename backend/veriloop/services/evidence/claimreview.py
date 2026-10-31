"""ClaimReview fast path (SPEC §3, D-15).

1. `search_feed`: keyless local index of Google's public ClaimReview feed (tool `claimreview_feed`):
   Postgres FTS top-50 + HNSW nearest neighbours -> embedding rerank (missing vectors are embedded and
   stored on the fly) -> keep rescaled similarity >= 0.6.
2. `search_factcheck_api`: Google Fact Check Tools `claims:search` (tool `factcheck`), only when
   GOOGLE_FACTCHECK_API_KEY is set.
3. `fetch_factcheck_page` + `claimreviews_from_jsonld`: keyless recovery for fact-check articles
   found via news RSS (Alt News, BOOM, PIB... are barely in the feed): schema.org ClaimReview
   JSON-LD on the page; otherwise the verdict is read from headline words (lexicon).
"""

from __future__ import annotations

import json
import re
from typing import Any

import numpy as np
import structlog
from bs4 import BeautifulSoup

from veriloop.contracts import ClaimReviewInfo, RetrievalMethod, ToolError
from veriloop.core.errors import MLUnavailable, ToolAttemptError
from veriloop.core.http import raise_for_status
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.evidence.feed_parse import parse_date
from veriloop.services.evidence.lexicon import (
    DETECTED_SUFFIX,
    detect_verdict,
    normalize_rating,
    verdict_to_rating,
)
from veriloop.services.evidence.lists import factchecker_for, host_of
from veriloop.services.evidence.models import Candidate, SourceOutcome
from veriloop.services.evidence.sources import BROWSER_UA, tool_call
from veriloop.services.evidence.text import claim_match, keywords

log = structlog.get_logger(__name__)

FEED_MIN_SIMILARITY = 0.6
EMBED_TEXT_CHARS = 512


def _info(
    publisher: str, rating_text: str, claim_text: str | None, claimant: str | None, similarity: float
) -> ClaimReviewInfo:
    norm = normalize_rating(rating_text)
    return ClaimReviewInfo(
        publisher=publisher,
        rating_text=rating_text,
        normalized_rating=norm.value,
        manipulation_rating=norm.manipulation,
        claim_text=claim_text,
        claimant=claimant,
        similarity=round(similarity, 4),
    )


def review_snippet(claim_text: str, rating_text: str, publisher: str) -> str:
    return f"Claim reviewed: {claim_text} — Rating: {rating_text} ({publisher})"


# ---- 1. local feed index --------------------------------------------------------------------------
async def search_feed(
    deps: Deps,
    texts: list[str],
    query_vecs: np.ndarray | None,
    passage_vecs: np.ndarray | None = None,
    *,
    limit: int = 6,
    timeout_s: float = 5.0,
) -> SourceOutcome:
    """`query_vecs` / `passage_vecs`: the claim text(s) embedded with the e5 query / passage prefix
    (rows = texts). Similarity = text.claim_match over both scales, max over the claim texts."""
    texts = [t for t in dict.fromkeys(texts) if t and t.strip()]
    key = {"q": texts, "limit": limit}
    # FTS on content words only: the OR-query over a full sentence matches ~25k rows and ranking them
    # took 0.2-1.0 s; the top-10 keywords take 30-215 ms with the same top-50 (measured 2026-10-31).
    fts_texts = list(dict.fromkeys(" ".join(keywords(t, limit=10)) or t for t in texts))
    qmat = _as_matrix(query_vecs)
    pmat = _as_matrix(passage_vecs)

    async def run() -> list[dict[str, Any]]:
        cands: dict[str, repo.ClaimReviewHit] = {}
        for text in fts_texts:
            for hit in await repo.search_claimreviews_fts(deps.db, text, limit=50):
                cands.setdefault(hit.record.id, hit)
        # HNSW neighbours catch cross-lingual matches FTS cannot (Hindi claim vs English review).
        for mat in (qmat, pmat):
            for vec in mat if mat is not None else []:
                for hit in await repo.search_claimreviews_by_embedding(deps.db, vec, limit=15):
                    cands.setdefault(hit.record.id, hit)
        if not cands:
            return []
        if qmat is None and pmat is None:  # no embedder: lexical rank only, similarity unknown -> 0
            return [_row(h, 0.0) for h in list(cands.values())[:limit]]
        missing = [h for h in cands.values() if h.embedding is None]
        if missing:
            try:
                vecs = await deps.embedder.embed(
                    [h.record.claim_text[:EMBED_TEXT_CHARS] for h in missing], kind="passage"
                )
            except MLUnavailable:
                vecs = None  # rerank only what is already embedded
            if vecs is not None:
                for h, v in zip(missing, vecs, strict=True):
                    h.embedding = v
                await repo.set_claimreview_embeddings(deps.db, [(h.record.id, h.embedding) for h in missing])
        scored = sorted(
            (
                (match_similarity(h.embedding, qmat, pmat), h)
                for h in cands.values()
                if h.embedding is not None
            ),
            key=lambda x: (-x[0], x[1].record.id),
        )
        return [_row(h, sim) for sim, h in scored[:limit] if sim >= FEED_MIN_SIMILARITY]

    value, outcome = await tool_call(
        deps, "claimreview_feed", "search", key, run, timeout_s=timeout_s, retries=0
    )
    if value is None:
        return outcome
    for row in value:
        url = row["review_url"]
        host = host_of(row.get("publisher_site") or url) or host_of(url)
        publisher = row.get("publisher_name") or host
        rating = row.get("rating_text") or ""
        outcome.returned_urls.add(url)
        outcome.candidates.append(
            Candidate(
                source_name=publisher,
                url=url,
                host=host_of(url) or host,
                title=f"Fact check ({publisher}): {row['claim_text'][:160]}",
                snippet=review_snippet(row["claim_text"], rating or "unrated", publisher),
                retrieval_method=RetrievalMethod.FACTCHECK_CLAIMSEARCH,
                tool="claimreview_feed",
                published_at=parse_date(row.get("review_date")),
                language=row.get("language"),
                claimreview=_info(
                    publisher, rating or "unrated", row["claim_text"], row.get("claimant"), row["similarity"]
                ),
                rating_method="feed",
                is_fact_check=True,
                pinned=True,
            )
        )
    return outcome


def _as_matrix(vecs: np.ndarray | None) -> np.ndarray | None:
    if vecs is None:
        return None
    mat = np.asarray(vecs, dtype=np.float32)
    mat = mat.reshape(1, -1) if mat.ndim == 1 else mat
    return mat if len(mat) else None


def match_similarity(passage_vec: np.ndarray, qmat: np.ndarray | None, pmat: np.ndarray | None) -> float:
    """Similarity of a reviewed claim (passage-embedded) to our claim texts: claim_match, max over texts."""
    qmat, pmat = _as_matrix(qmat), _as_matrix(pmat)
    qp = float(np.max(qmat @ passage_vec)) if qmat is not None else None
    pp = float(np.max(pmat @ passage_vec)) if pmat is not None else None
    return round(claim_match(pp, qp), 4)


def _row(hit: repo.ClaimReviewHit, similarity: float) -> dict[str, Any]:
    r = hit.record
    return {
        "id": r.id,
        "claim_text": r.claim_text,
        "claimant": r.claimant,
        "review_url": r.review_url,
        "publisher_name": r.publisher_name,
        "publisher_site": r.publisher_site,
        "rating_text": r.rating_text,
        "review_date": r.review_date.isoformat() if r.review_date else None,
        "language": r.language,
        "similarity": round(similarity, 4),
    }


# ---- 2. Google Fact Check Tools API (keyed) -----------------------------------------------------
async def search_factcheck_api(
    deps: Deps, query: str, language: str, *, timeout_s: float = 5.0
) -> SourceOutcome:
    params = {"query": query[:500], "languageCode": language or "en", "pageSize": "10"}

    async def fetch() -> Any:
        resp = await deps.tools.http.get(
            "https://factchecktools.googleapis.com/v1alpha1/claims:search",
            params={**params, "key": deps.settings.google_factcheck_api_key.get_secret_value()},
        )
        raise_for_status(resp)
        return resp.json()

    value, outcome = await tool_call(
        deps, "factcheck", "claims_search", params, fetch, timeout_s=timeout_s, retries=1
    )
    if value is None:
        return outcome
    for claim in (value or {}).get("claims") or []:
        text = claim.get("text") or ""
        for rv in claim.get("claimReview") or []:
            url = rv.get("url")
            if not url or not text:
                continue
            pub = rv.get("publisher") or {}
            publisher = pub.get("name") or pub.get("site") or host_of(url)
            rating = rv.get("textualRating") or "unrated"
            outcome.returned_urls.add(url)
            outcome.candidates.append(
                Candidate(
                    source_name=publisher,
                    url=url,
                    host=host_of(url),
                    title=rv.get("title") or f"Fact check ({publisher})",
                    snippet=review_snippet(text, rating, publisher),
                    retrieval_method=RetrievalMethod.FACTCHECK_CLAIMSEARCH,
                    tool="factcheck",
                    published_at=parse_date(rv.get("reviewDate")),
                    language=rv.get("languageCode"),
                    claimreview=_info(publisher, rating, text, claim.get("claimant"), 0.0),  # sim set later
                    rating_method="factcheck_api",
                    is_fact_check=True,
                    pinned=True,
                )
            )
    return outcome


# ---- 3. fact-check pages: JSON-LD ClaimReview, else headline verdict words ---------------------------
_CLOUDFLARE = re.compile(r"<title>\s*Just a moment|cf-browser-verification|challenge-platform", re.IGNORECASE)


def _walk(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, list):
        for n in node:
            out.extend(_walk(n))
    elif isinstance(node, dict):
        out.append(node)
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                out.extend(_walk(node[key]))
    return out


def _types(node: dict[str, Any]) -> set[str]:
    t = node.get("@type")
    return {str(x) for x in t} if isinstance(t, list) else {str(t)} if t else set()


def extract_page_data(html: str) -> dict[str, Any]:
    """ClaimReview nodes + article date/title from a fact-check page (pure)."""
    soup = BeautifulSoup(html, "lxml")
    reviews: list[dict[str, Any]] = []
    published: str | None = None
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        for node in _walk(data):
            types = _types(node)
            if "ClaimReview" in types:
                rating = node.get("reviewRating") if isinstance(node.get("reviewRating"), dict) else {}
                item = node.get("itemReviewed") if isinstance(node.get("itemReviewed"), dict) else {}
                author = node.get("author")
                reviews.append(
                    {
                        "claimReviewed": node.get("claimReviewed"),
                        "rating": rating.get("alternateName") or rating.get("name"),
                        "author": author.get("name") if isinstance(author, dict) else author,
                        "url": node.get("url"),
                        "datePublished": node.get("datePublished"),
                        "claimant": (item.get("author") or {}).get("name")
                        if isinstance(item.get("author"), dict)
                        else None,
                    }
                )
            if published is None and types & {
                "NewsArticle",
                "Article",
                "BlogPosting",
                "ReportageNewsArticle",
            }:
                published = node.get("datePublished") if isinstance(node.get("datePublished"), str) else None
    if published is None:
        meta = soup.find("meta", attrs={"property": "article:published_time"})
        published = meta.get("content") if meta else None
    og = soup.find("meta", attrs={"property": "og:title"})
    return {"claimreviews": reviews[:5], "published": published, "title": og.get("content") if og else None}


async def fetch_factcheck_page(
    deps: Deps, url: str, *, timeout_s: float = 4.0
) -> tuple[dict[str, Any] | None, ToolError | None]:
    async def fetch() -> dict[str, Any]:
        resp = await deps.tools.http.get(url, headers=BROWSER_UA)
        raise_for_status(resp)
        text = resp.text[:2_000_000]
        if _CLOUDFLARE.search(text[:5000]):
            raise ToolAttemptError("blocked", "bot challenge page (HTTP 200)")
        return {"url": str(resp.url), **extract_page_data(text)}

    value, outcome = await tool_call(
        deps,
        "page_fetch",
        "factcheck_page",
        {"url": url},
        fetch,
        timeout_s=timeout_s,
        retries=0,
        cache_ttl_s=86400,
    )
    return value, outcome.error


def apply_page_rating(cand: Candidate, page: dict[str, Any] | None) -> bool:
    """Prefer the page's own ClaimReview JSON-LD; returns True when it set the rating."""
    if not page:
        return False
    for rv in page.get("claimreviews") or []:
        rating = rv.get("rating")
        if not isinstance(rating, str) or not rating.strip():
            continue
        checker = factchecker_for(cand.host, url=cand.url, title=cand.title)
        publisher = (rv.get("author") if isinstance(rv.get("author"), str) else None) or (
            checker.name if checker else cand.source_name
        )
        claim_text = rv.get("claimReviewed") if isinstance(rv.get("claimReviewed"), str) else None
        cand.claimreview = _info(publisher, rating.strip(), claim_text, rv.get("claimant"), 0.0)
        cand.rating_method = "jsonld"
        break
    published = parse_date(page.get("published"))
    if published is not None:
        cand.published_at = published  # the page's date beats Bing/RSS dates (research.md)
    return cand.rating_method == "jsonld"


def apply_headline_rating(cand: Candidate) -> bool:
    """No JSON-LD: infer the verdict from headline/snippet words of a fact-check article."""
    found = detect_verdict(cand.title) or detect_verdict(cand.snippet)
    if found is None:
        return False
    checker = factchecker_for(cand.host, url=cand.url, title=cand.title)
    publisher = checker.name if checker else cand.source_name
    value = verdict_to_rating(found.verdict)
    norm_manip = found.verdict.value == "manipulated"
    cand.claimreview = ClaimReviewInfo(
        publisher=publisher,
        rating_text=f"{found.word}{DETECTED_SUFFIX}",
        normalized_rating=value,
        manipulation_rating=norm_manip,
        claim_text=None,
        similarity=0.0,  # set by the pipeline from the headline embedding
    )
    cand.rating_method = "headline"
    return True
