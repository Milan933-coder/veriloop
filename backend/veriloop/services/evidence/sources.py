"""Keyless and keyed search sources. Every external call goes through `deps.tools.call` (D-12).

Each `search_*` returns a SourceOutcome: candidates parsed from the tool response, the ToolError on
failure (never fabricated data, G3), and the set of URLs the response contained (for the URL
validator). Tool fns return raw text/JSON; parsing happens after the call so caches and cassettes
replay exactly what the live call returned.

RetrievalMethod mapping (the contract enum has no Bing / Wikipedia / feed members; `tool` always
names the exact source): Bing News RSS -> SERPER_NEWS and Wikipedia -> SERPER_WEB (D-15: they are
the keyless substitutes for the general-search API), ClaimReview feed -> FACTCHECK_CLAIMSEARCH.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import structlog

from veriloop.contracts import RetrievalMethod, ToolError
from veriloop.core.errors import ToolAttemptError
from veriloop.core.http import raise_for_status
from veriloop.deps import Deps
from veriloop.services.evidence import rss
from veriloop.services.evidence.lists import host_of, is_government
from veriloop.services.evidence.models import Candidate, SourceOutcome
from veriloop.services.evidence.text import google_news_locale, keywords, strip_html

log = structlog.get_logger(__name__)

BROWSER_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}
WIRE_SITES = ("reuters.com", "apnews.com", "ptinews.com", "aninews.in")  # D-09
PIB_FEED = "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3&reg=3"  # &reg=3: English (research.md)
GDELT_MIN_INTERVAL_S = 6.0  # GDELT asks for one request per 5 s; shared IP -> be stricter

_gdelt_last_call = 0.0


async def tool_call(
    deps: Deps,
    tool: str,
    op: str,
    key: dict[str, Any],
    fn: Callable[[], Awaitable[Any]],
    *,
    timeout_s: float | None = None,
    retries: int | None = None,
    cache_ttl_s: int | None = None,
) -> tuple[Any, SourceOutcome]:
    result = await deps.tools.call(
        tool, op, key, fn, timeout_s=timeout_s, retries=retries, cache_ttl_s=cache_ttl_s
    )
    outcome = SourceOutcome(tool=tool, op=op, latency_ms=result.latency_ms, from_cache=result.from_cache)
    if not result.ok:
        outcome.error = result.error
        return None, outcome
    return result.value, outcome


def _http(deps: Deps) -> Any:
    return deps.tools.http


# ---- Google News RSS (+ wire via site: filters, + PIB via site:pib.gov.in) ------------------------
async def search_gnews(
    deps: Deps,
    query: str,
    locale: str,
    *,
    tool: str = "gnews_rss",
    method: RetrievalMethod = RetrievalMethod.GOOGLE_NEWS_RSS,
    op: str = "search",
    limit: int = 40,
    timeout_s: float = 5.0,
) -> SourceOutcome:
    hl, gl, ceid = google_news_locale(locale)
    params = {"q": query, "hl": hl, "gl": gl, "ceid": ceid}

    async def fetch() -> str:
        resp = await _http(deps).get("https://news.google.com/rss/search", params=params, headers=BROWSER_UA)
        raise_for_status(resp)
        return resp.text

    value, outcome = await tool_call(deps, tool, op, params, fetch, timeout_s=timeout_s, retries=1)
    if value is None:
        return outcome
    for row in rss.parse_gnews(value)[:limit]:
        host = host_of(row["source_url"]) if row["source_url"] else ""
        if not host:
            continue  # without the publisher domain the item cannot be grouped (G4)
        outcome.returned_urls.add(row["link"])
        outcome.candidates.append(
            Candidate(
                source_name=row["source_name"] or host,
                url=row["link"],
                host=host,
                title=row["title"],
                snippet=row["title"],  # Google News descriptions only repeat the title + publisher
                retrieval_method=method,
                tool=tool,
                published_at=row["published_at"],
                language=hl.split("-")[0],
                gnews_link=row["link"],
                is_government=is_government(host),
            )
        )
    return outcome


async def search_wire(deps: Deps, query: str, locale: str) -> SourceOutcome:
    sites = " OR ".join(f"site:{s}" for s in WIRE_SITES)
    return await search_gnews(
        deps, f"{query} ({sites})", locale, tool="wire_rss", method=RetrievalMethod.WIRE_RSS, limit=15
    )


# ---- Google News link decoding (2 calls, undocumented; research.md / gnd.py) ----------------------
_SG = re.compile(r'data-n-a-sg="([^"]+)"')
_TS = re.compile(r'data-n-a-ts="([^"]+)"')
_RES = re.compile(r'\[\\"garturlres\\",\\"(.*?)\\"')


def gnews_article_id(link: str) -> str | None:
    try:
        parts = urlsplit(link)
    except ValueError:
        return None
    if not parts.hostname or not parts.hostname.endswith("news.google.com"):
        return None
    gid = parts.path.rstrip("/").split("/")[-1]
    return gid or None


async def decode_gnews(
    deps: Deps, link: str, *, timeout_s: float = 4.0
) -> tuple[str | None, ToolError | None]:
    """news.google.com/rss/articles/<id> -> publisher URL, or (None, error). Cached for 7 days."""
    gid = gnews_article_id(link)
    if not gid:
        return None, None

    async def fetch() -> dict[str, str]:
        http = _http(deps)
        page = await http.get(f"https://news.google.com/rss/articles/{gid}", headers=BROWSER_UA)
        raise_for_status(page)
        sg, ts = _SG.search(page.text), _TS.search(page.text)
        if not (sg and ts):
            raise ToolAttemptError("parse_error", "decode tokens not found on the article page")
        inner = json.dumps(
            [
                "garturlreq",
                [
                    [
                        "X",
                        "X",
                        ["X", "X"],
                        None,
                        None,
                        1,
                        1,
                        "US:en",
                        None,
                        1,
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                        1,
                    ],
                    "X",
                    "X",
                    1,
                    [1, 1, 1],
                    1,
                    1,
                    None,
                    0,
                    0,
                    None,
                    0,
                ],
                gid,
                int(ts.group(1)),
                sg.group(1),
            ]
        )
        body = {"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}
        resp = await http.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=body,
            headers={**BROWSER_UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        raise_for_status(resp)
        m = _RES.search(resp.text)
        if not m:
            raise ToolAttemptError("parse_error", "garturlres missing in batchexecute response")
        url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").replace("\\", "")
        if not url.startswith(("http://", "https://")):
            raise ToolAttemptError("parse_error", "decoded value is not a URL")
        return {"url": url}

    value, outcome = await tool_call(
        deps, "gnews_rss", "decode", {"id": gid}, fetch, timeout_s=timeout_s, retries=0, cache_ttl_s=7 * 86400
    )
    if value is None:
        return None, outcome.error
    return value.get("url"), None


# ---- Bing News RSS ------------------------------------------------------------------------------
async def search_bing(deps: Deps, query: str, locale: str, *, timeout_s: float = 5.0) -> SourceOutcome:
    market = locale if re.match(r"^[a-z]{2}-[A-Z]{2}$", locale or "") else "en-IN"
    params = {"q": query, "format": "rss", "mkt": market}

    async def fetch() -> str:
        resp = await _http(deps).get("https://www.bing.com/news/search", params=params, headers=BROWSER_UA)
        raise_for_status(resp)
        return resp.text

    value, outcome = await tool_call(
        deps, "bing_news_rss", "search", params, fetch, timeout_s=timeout_s, retries=1
    )
    if value is None:
        return outcome
    for row in rss.parse_bing(value):
        host = host_of(row["url"])
        outcome.returned_urls.add(row["url"])
        desc = row["description"]
        # Bing has a real snippet; keep the headline in it so stance quotes may come from either.
        snippet = (
            desc if desc.lower().startswith(row["title"].lower()[:40]) else f"{row['title']}. {desc}".strip()
        )
        outcome.candidates.append(
            Candidate(
                source_name=row["source_name"] or host,
                url=row["url"],
                host=host,
                title=row["title"],
                snippet=snippet,
                retrieval_method=RetrievalMethod.SERPER_NEWS,
                tool="bing_news_rss",
                published_at=row["published_at"],
                language=market.split("-")[0],
                is_government=is_government(host),
            )
        )
    return outcome


# ---- PIB RSS ------------------------------------------------------------------------------------
async def fetch_pib(deps: Deps, *, timeout_s: float = 5.0) -> SourceOutcome:
    async def fetch() -> str:
        resp = await _http(deps).get(PIB_FEED, headers=BROWSER_UA)
        raise_for_status(resp)
        return resp.text

    value, outcome = await tool_call(
        deps, "pib_rss", "feed", {"url": PIB_FEED}, fetch, timeout_s=timeout_s, retries=1
    )
    if value is None:
        return outcome
    for row in rss.parse_pib(value):
        outcome.returned_urls.add(row["url"])
        outcome.candidates.append(
            Candidate(
                source_name="Press Information Bureau (PIB)",
                url=row["url"],
                host=host_of(row["url"]),
                title=row["title"],
                snippet=row["title"],
                retrieval_method=RetrievalMethod.PIB_RSS,
                tool="pib_rss",
                published_at=None,  # the feed has no dates (research.md)
                language="en",
                is_government=True,
            )
        )
    return outcome


# ---- GDELT DOC 2.0 ------------------------------------------------------------------------------
def gdelt_query(terms: list[str]) -> str | None:
    """AND of up to 4 terms; hyphenated terms quoted (GDELT rejects them unquoted); short ones dropped."""
    picked: list[str] = []
    for term in terms:
        t = term.strip().strip('"')
        if len(t) < 3 or not re.search(r"\w", t):
            continue
        if re.search(r"[^\w]", t):
            t = f'"{t}"'
        if t.lower() not in (p.lower() for p in picked):
            picked.append(t)
        if len(picked) >= 4:
            break
    return " ".join(picked) if len(picked) >= 2 else None


async def search_gdelt(deps: Deps, terms: list[str], *, timeout_s: float = 6.0) -> SourceOutcome:
    global _gdelt_last_call
    query = gdelt_query(terms)
    outcome = SourceOutcome(tool="gdelt", op="artlist")
    if not query:
        outcome.note = "gdelt skipped: not enough query terms"
        return outcome
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": "50",
        "timespan": "3months",
        "sort": "hybridrel",
    }
    now = time.monotonic()
    cached = await deps.tools.cache.get("gdelt", _key_hash("artlist", params))
    if not cached[0] and now - _gdelt_last_call < GDELT_MIN_INTERVAL_S:
        outcome.note = "gdelt skipped: global 1 request / 6 s budget (corroboration only, never required)"
        return outcome
    _gdelt_last_call = now

    async def fetch() -> str:
        resp = await _http(deps).get("https://api.gdeltproject.org/api/v2/doc/doc", params=params)
        raise_for_status(resp)
        text = resp.text.strip()
        if not text.startswith("{"):
            # HTTP 200 plain-text errors: rate limit or query syntax. Never cache them as success.
            kind = "rate_limited" if "limit requests" in text.lower() else "parse_error"
            raise ToolAttemptError(kind, f"GDELT: {text[:160]}")
        return text

    value, outcome = await tool_call(deps, "gdelt", "artlist", params, fetch, timeout_s=timeout_s, retries=0)
    if value is None:
        return outcome
    try:
        rows = rss.parse_gdelt(value)
    except rss.GdeltError as exc:
        outcome.error = ToolError(
            tool="gdelt", op="artlist", error_type="parse_error", message=str(exc)[:300]
        )
        return outcome
    for row in rows:
        host = host_of(row["url"])
        outcome.returned_urls.add(row["url"])
        outcome.candidates.append(
            Candidate(
                source_name=row["domain"] or host,
                url=row["url"],
                host=host,
                title=row["title"],
                snippet=row["title"],
                retrieval_method=RetrievalMethod.GDELT_DOC,
                tool="gdelt",
                published_at=row["published_at"],
                language=(row["language"] or "")[:2].lower() or None,
                is_government=is_government(host),
            )
        )
    return outcome


def _key_hash(op: str, key: dict[str, Any]) -> str:
    from veriloop.core.tool_client import call_key_hash

    return call_key_hash(op, key)


# ---- Wikipedia ----------------------------------------------------------------------------------
async def search_wikipedia(
    deps: Deps, query: str, *, lang: str = "en", timeout_s: float = 5.0
) -> SourceOutcome:
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrlimit": "4",
        "prop": "info|extracts",
        "inprop": "url",
        "exintro": "1",
        "explaintext": "1",
        "exsentences": "3",
        "exlimit": "4",
        "format": "json",
        "formatversion": "2",
    }
    endpoint = f"https://{lang}.wikipedia.org/w/api.php"

    async def fetch() -> Any:
        resp = await _http(deps).get(
            endpoint, params=params, headers={"User-Agent": deps.settings.wikidata_user_agent}
        )
        raise_for_status(resp)
        return resp.json()

    value, outcome = await tool_call(
        deps, "wikipedia", "search", {"endpoint": endpoint, **params}, fetch, timeout_s=timeout_s, retries=1
    )
    if value is None:
        return outcome
    pages = ((value or {}).get("query") or {}).get("pages") or []
    for page in sorted(pages, key=lambda p: p.get("index", 99)):
        url = page.get("fullurl") or page.get("canonicalurl")
        extract = " ".join((page.get("extract") or "").split())
        if not url or not extract:
            continue
        outcome.returned_urls.add(url)
        outcome.candidates.append(
            Candidate(
                source_name=f"Wikipedia ({lang})",
                url=url,
                host=host_of(url),
                title=page.get("title") or "",
                snippet=extract[:600],
                retrieval_method=RetrievalMethod.SERPER_WEB,
                tool="wikipedia",
                language=lang,
            )
        )
    return outcome


# ---- Serper (keyed; D-04) -----------------------------------------------------------------------
async def search_serper(
    deps: Deps, query: str, locale: str, *, kind: str = "news", timeout_s: float = 5.0
) -> SourceOutcome:
    hl, gl, _ = google_news_locale(locale)
    body = {"q": query, "gl": gl.lower(), "hl": hl.split("-")[0], "num": 10}

    async def fetch() -> Any:
        api_key = deps.settings.serper_api_key.get_secret_value()
        resp = await _http(deps).post(
            f"https://google.serper.dev/{kind}", json=body, headers={"X-API-KEY": api_key}
        )
        raise_for_status(resp)
        return resp.json()

    value, outcome = await tool_call(deps, "serper", kind, body, fetch, timeout_s=timeout_s, retries=1)
    if value is None:
        return outcome
    rows = (value or {}).get("news" if kind == "news" else "organic") or []
    for row in rows:
        url = row.get("link")
        title = strip_html(row.get("title") or "")
        if not url or not title:
            continue
        host = host_of(url)
        outcome.returned_urls.add(url)
        snippet = strip_html(row.get("snippet") or "")
        outcome.candidates.append(
            Candidate(
                source_name=row.get("source") or host,
                url=url,
                host=host,
                title=title,
                snippet=f"{title}. {snippet}".strip() if snippet else title,
                retrieval_method=RetrievalMethod.SERPER_NEWS
                if kind == "news"
                else RetrievalMethod.SERPER_WEB,
                tool="serper",
                is_government=is_government(host),
            )
        )
    return outcome


def entity_terms(entities: list[str], fallback: str) -> list[str]:
    terms: list[str] = []
    for e in entities:
        terms.append(e)
    terms.extend(keywords(fallback, limit=8))
    return terms
