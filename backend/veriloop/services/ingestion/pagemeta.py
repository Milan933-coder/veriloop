"""Parsing of page metadata and oEmbed payloads (pure functions, no I/O).

Everything here only *copies* values present in a fetched document; nothing is inferred (G3).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from veriloop.contracts import Platform

OEMBED_ENDPOINTS: dict[Platform, str] = {
    Platform.YOUTUBE: "https://www.youtube.com/oembed",
    Platform.X: "https://publish.twitter.com/oembed",
    Platform.TIKTOK: "https://www.tiktok.com/oembed",
}

# Pages that answer HTTP 200 but are really a bot wall / login wall (research.md: ThePrint, Instagram).
_BLOCK_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "verify you are human",
)
_LOGIN_MARKERS = (
    "log in to instagram",
    "login • instagram",
    "log into facebook",
    "log in to facebook",
    "you must log in",
    "sign in to continue",
    "this content isn't available",
    "content not available",
)


def oembed_request(platform: Platform, url: str) -> tuple[str, dict[str, str]] | None:
    endpoint = OEMBED_ENDPOINTS.get(platform)
    if endpoint is None:
        return None
    params = {"url": url, "format": "json"}
    if platform is Platform.X:
        params = {"url": url, "omit_script": "1", "dnt": "true"}
    return endpoint, params


def oembed_full_url(platform: Platform, url: str) -> str | None:
    req = oembed_request(platform, url)
    return None if req is None else f"{req[0]}?{urlencode(req[1])}"


def parse_date(value: Any) -> datetime | None:
    """Lenient date parse for meta/oEmbed values; naive results are taken as UTC. None if unparseable."""
    if value is None:
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):  # yt-dlp upload_date YYYYMMDD
        try:
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    try:
        parsed = date_parser.parse(text)
    except (ValueError, OverflowError, TypeError):
        return None
    if not 1990 <= parsed.year <= 2100:  # ClaimReview-style garbage years (research.md)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node is not None else ""


def parse_x_oembed_html(snippet: str) -> dict[str, Any]:
    """Tweet text, author line and date from publish.twitter.com's blockquote markup."""
    soup = BeautifulSoup(snippet or "", "lxml")
    out: dict[str, Any] = {}
    quote = soup.find("blockquote")
    if quote is None:
        return out
    para = quote.find("p")
    if para is not None:
        # pic.twitter.com / t.co links are attachments, not caption text.
        for a in para.find_all("a"):
            if a.get_text(strip=True).startswith(("pic.twitter.com", "https://t.co", "http://t.co")):
                a.decompose()
        for br in para.find_all("br"):
            br.replace_with("\n")
        out["text"] = html_lib.unescape(para.get_text("", strip=False)).strip()
        out["lang"] = para.get("lang")
    anchors = quote.find_all("a")
    if anchors:
        out["date_text"] = anchors[-1].get_text(strip=True)
    return out


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag is not None:
            content = (tag.get("content") or "").strip()
            if content:
                return html_lib.unescape(content)
    return None


def _json_ld_dates(soup: BeautifulSoup) -> str | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                for key in ("datePublished", "uploadDate", "dateCreated"):
                    if isinstance(node.get(key), str):
                        return node[key]
                stack.extend(v for v in node.values() if isinstance(v, dict | list))
            elif isinstance(node, list):
                stack.extend(node)
    return None


def page_status(html: str) -> str:
    """'blocked' (bot wall), 'login' (login wall) or 'ok'. Looks only at the head of the document."""
    head = (html or "")[:20000].lower()
    if any(m in head for m in _BLOCK_MARKERS):
        return "blocked"
    if any(m in head for m in _LOGIN_MARKERS):
        return "login"
    return "ok"


def parse_page_meta(html: str, base_url: str) -> dict[str, Any]:
    """og:/twitter:/article: meta, <title>, JSON-LD dates. Image/video URLs are made absolute."""
    soup = BeautifulSoup(html or "", "lxml")
    title = _meta(soup, "og:title", "twitter:title") or _text(soup.find("title")) or None
    description = _meta(soup, "og:description", "twitter:description", "description")
    image = _meta(soup, "og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
    video = _meta(soup, "og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream")
    published = _meta(
        soup, "article:published_time", "og:published_time", "datePublished", "pubdate", "date"
    ) or _json_ld_dates(soup)
    author = _meta(soup, "author", "article:author", "twitter:creator", "og:site_name")
    return {
        "title": title,
        "description": description,
        "image": urljoin(base_url, image) if image else None,
        "video": urljoin(base_url, video) if video else None,
        "published": published,
        "author": author,
        "site_name": _meta(soup, "og:site_name"),
        "og_type": _meta(soup, "og:type"),
        "status": page_status(html),
    }


_PROMO_LINE = re.compile(
    r"(subscribe|follow us|watch more|sign up|newsletter|donate|support us|download the app|"
    r"all rights reserved|like and share|join our|whatsapp channel)",
    re.IGNORECASE,
)


def clean_caption(text: str | None, *, max_chars: int = 2000) -> str | None:
    """Drop link-only and promo lines from platform descriptions (YouTube boilerplate), keep content."""
    if not text:
        return None
    lines: list[str] = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or set(line) <= set("-–—=_*•|~"):
            continue
        stripped = re.sub(r"https?://\S+", "", line).strip(" :-–—|•")
        if not stripped or _PROMO_LINE.search(line):
            continue
        lines.append(line)
    out = "\n".join(lines).strip()
    return out[:max_chars] or None
