"""Pure parsers for the RSS / JSON payloads returned by the news tools (docs/research.md formats).

Tool fns return the raw response text; these functions run after ToolClient so live, cached and
replayed responses parse identically. XML is parsed with lxml in recover mode with entity
resolution and network access disabled (untrusted input).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from lxml import etree

from veriloop.services.evidence.text import strip_html

_PARSER = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)


def _root(xml: str) -> Any:
    data = (xml or "").strip().encode("utf-8")
    if not data:
        return None
    try:
        return etree.fromstring(data, parser=_PARSER)
    except (etree.XMLSyntaxError, ValueError):
        return None


def _local(tag: Any) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child(el: Any, name: str) -> Any:
    for c in el:
        if _local(c.tag).lower() == name.lower():
            return c
    return None


def _text(el: Any, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None and c.text else ""


def parse_rfc822(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _items(xml: str) -> list[Any]:
    root = _root(xml)
    if root is None:
        return []
    return [el for el in root.iter() if _local(el.tag) == "item"]


def parse_gnews(xml: str) -> list[dict[str, Any]]:
    """Google News RSS: title ("Headline - Publisher"), encoded link, pubDate, <source url=..>."""
    out = []
    for it in _items(xml):
        source = _child(it, "source")
        source_name = (source.text or "").strip() if source is not None and source.text else ""
        source_url = source.get("url", "") if source is not None else ""
        title = strip_html(_text(it, "title"))
        suffix = f" - {source_name}"
        if source_name and title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        link = _text(it, "link")
        if not title or not link:
            continue
        out.append(
            {
                "title": title,
                "link": link,
                "published_at": parse_rfc822(_text(it, "pubDate")),
                "source_name": source_name,
                "source_url": source_url,
            }
        )
    return out


def bing_target_url(link: str) -> str | None:
    """bing.com/news/apiclick.aspx?...&url=<publisher URL> -> publisher URL (research.md)."""
    try:
        parts = urlsplit(link)
    except ValueError:
        return None
    if parts.hostname and parts.hostname.endswith("bing.com"):
        target = parse_qs(parts.query).get("url", [None])[0]
        return target if target and target.startswith(("http://", "https://")) else None
    return link if link.startswith(("http://", "https://")) else None


def parse_bing(xml: str) -> list[dict[str, Any]]:
    """Bing News RSS (max 12 items): real publisher URL in the url= param, a real snippet, News:Source."""
    out = []
    for it in _items(xml):
        link = _text(it, "link")
        url = bing_target_url(link)
        title = strip_html(_text(it, "title"))
        if not url or not title:
            continue
        out.append(
            {
                "title": title,
                "url": url,
                "description": strip_html(_text(it, "description")),
                "published_at": parse_rfc822(_text(it, "pubDate")),
                "source_name": _text(it, "Source"),
            }
        )
    return out


def parse_pib(xml: str) -> list[dict[str, Any]]:
    """PIB RSS (&reg=3): title + link only, no date or description (research.md)."""
    out = []
    for it in _items(xml):
        title = strip_html(_text(it, "title"))
        link = _text(it, "link")
        if title and link.startswith(("http://", "https://")):
            out.append({"title": title, "url": link})
    return out


class GdeltError(ValueError):
    pass


def parse_gdelt(payload: Any) -> list[dict[str, Any]]:
    """GDELT DOC 2.0 artlist. Errors arrive as HTTP 200 plain text ("phrase is too short", rate limits)."""
    if isinstance(payload, str):
        text = payload.strip()
        if not text.startswith("{"):
            raise GdeltError(f"GDELT returned text instead of JSON: {text[:160]}")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise GdeltError(f"GDELT returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GdeltError("GDELT payload is not an object")
    out = []
    for art in payload.get("articles") or []:
        url = art.get("url")
        title = (art.get("title") or "").strip()
        if not url or not title:
            continue
        seen = art.get("seendate") or ""
        try:
            published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) if seen else None
        except ValueError:
            published = None
        out.append(
            {
                "url": url,
                "title": title,
                "domain": art.get("domain") or "",
                "language": art.get("language") or None,
                "published_at": published,
                "sourcecountry": art.get("sourcecountry") or None,
            }
        )
    return out
