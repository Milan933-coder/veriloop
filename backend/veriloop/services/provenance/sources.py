"""Domain helpers + context lists (satire / state-affiliated / known disinformation).

Publisher grouping and fact-checker detection reuse the evidence package's implementation when it is
importable, so a fact-check cited by both branches lands in ONE independence group (G4 counting);
the local fallback keeps provenance working on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
import tldextract

from veriloop.core.config import REPO_ROOT

log = structlog.get_logger(__name__)

LISTS_DIR = REPO_ROOT / "data" / "lists"
_EXTRACT = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=())  # bundled PSL snapshot: no network

CONTEXT_LISTS = {
    "satire": ("satire.json", "domains", "domain"),
    "state_affiliated": ("state_affiliated.json", "domains", "domain"),
    "known_disinfo": ("known_disinfo.json", "sources", "domain_or_handle"),
}


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "" if "://" in url else url.split("/", 1)[0]).lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def registrable_domain(url: str) -> str:
    try:
        from veriloop.services.evidence.lists import registrable_domain as ev_domain

        return ev_domain(url)
    except Exception:
        host = host_of(url)
        ext = _EXTRACT(host)
        top = getattr(ext, "top_domain_under_public_suffix", None) or ext.registered_domain
        return (top or host).lower()


def publisher_group(url: str) -> str:
    try:
        from veriloop.services.evidence.lists import publisher_group as ev_group

        return ev_group(url)
    except Exception:
        return registrable_domain(url) or "unknown"


@dataclass(frozen=True)
class Publisher:
    name: str
    is_fact_check: bool
    is_government: bool


def publisher_for(url: str) -> Publisher:
    domain = registrable_domain(url)
    try:
        from veriloop.services.evidence.lists import factchecker_for, is_government

        checker = factchecker_for(url, url=url)
        if checker is not None:
            return Publisher(checker.name, checker.kind == "factchecker", checker.kind == "govt_factcheck")
        return Publisher(domain, False, is_government(url))
    except Exception:
        return Publisher(domain, _fallback_factchecker(domain), domain.endswith(("gov.in", "nic.in")))


@lru_cache(maxsize=1)
def _fallback_checkers() -> frozenset[str]:
    try:
        data = json.loads((LISTS_DIR / "factcheckers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    out: set[str] = set()
    for entry in data.get("factcheckers") or []:
        out.add(str(entry.get("domain", "")).lower())
        out.update(str(d).lower() for d in entry.get("other_domains") or [])
    return frozenset(d for d in out if d)


def _fallback_factchecker(domain: str) -> bool:
    return domain in _fallback_checkers()


@dataclass(frozen=True)
class ContextHit:
    flag: str  # satire | state_affiliated | known_disinfo
    name: str
    domain: str
    source: str  # which list + its evidence link, shown to the user


@lru_cache(maxsize=1)
def _context_index() -> dict[str, list[ContextHit]]:
    index: dict[str, list[ContextHit]] = {}
    for flag, (filename, key, domain_field) in CONTEXT_LISTS.items():
        path = LISTS_DIR / filename
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("context_list_unreadable", path=str(path), error=str(exc))
            continue
        for entry in data.get(key) or []:
            names = [entry.get(domain_field), *(entry.get("aliases") or [])]
            evidence = entry.get("evidence_url") or next(iter(entry.get("evidence_urls") or []), "")
            for raw in names:
                domain = str(raw or "").lower().strip()
                if not domain or "." not in domain or domain.startswith("@"):
                    continue  # handles (e.g. X accounts) are not matchable as page domains
                hit = ContextHit(
                    flag, str(entry.get("name") or domain), domain, f"{filename}: {evidence}".strip()
                )
                index.setdefault(domain, []).append(hit)
    return index


def context_for(url: str) -> list[ContextHit]:
    """Context-list entries matching the URL's host or any parent domain."""
    host = host_of(url)
    if not host:
        return []
    index = _context_index()
    parts = host.split(".")
    hits: list[ContextHit] = []
    for i in range(len(parts) - 1):
        hits.extend(index.get(".".join(parts[i:]), []))
    return hits


def lists_dir() -> Path:
    return LISTS_DIR
