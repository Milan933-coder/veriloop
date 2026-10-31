"""Curated source lists (data/lists/*.json): fact-checkers, government sources, ownership groups.

These drive three decisions that must be deterministic and explainable in the audit trail:
* the independence key (`publisher_group`) used by the 2-group rule (G4),
* whether an item is a fact-check (ClaimReview-equivalent) and how much its publisher is trusted,
* whether an item is a government source (SPEC §3: flag government matches explicitly).

The lists are static files in the repo, loaded once (no network). Domain parsing uses tldextract's
bundled public-suffix snapshot (`suffix_list_urls=()`, no cache dir) so it never downloads or writes
to the (full) C: profile.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
import tldextract

from veriloop.core.config import REPO_ROOT

log = structlog.get_logger(__name__)

LISTS_DIR = REPO_ROOT / "data" / "lists"
AGGREGATOR_PREFIX = "aggregator:"
GOVT_INDIA = "govt_india"

# Outlets that publish fact-checks AND ordinary news: an item counts as a fact-check only when the URL
# path or the title says so (research.md: "Match path prefix /fact-check/ to distinguish fact checks").
_MIXED_OUTLETS = frozenset(
    {
        "thequint.com",
        "indiatoday.in",
        "thelallantop.com",
        "ptinews.com",
        "reuters.com",
        "dw.com",
        "usatoday.com",
        "aap.com.au",
        "manoramaonline.com",
        "telugupost.com",
        "medicaldialogues.in",
        "thelogicalindian.com",
        "pib.gov.in",
        "newsmeter.in",
    }
)
_FACTCHECK_PATH = re.compile(r"fact[-_ ]?check|webqoof", re.IGNORECASE)
_FACTCHECK_TITLE = re.compile(
    r"fact[\s-]?check|factcheck|webqoof|फैक्ट\s?चेक|फ़ैक्ट\s?चेक|पड़ताल|ফ্যাক্ট\s?চেক|ఫ్యాక్ట్\s?చెక్",
    re.IGNORECASE,
)

_EXTRACT = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=())


@dataclass(frozen=True)
class FactChecker:
    domain: str
    name: str
    kind: str  # factchecker | govt_factcheck
    ifcn_status: str
    reliability: float  # 0..1 weight for the claimreview signal (IFCN status, research.md notes)
    mixed: bool = False  # also publishes ordinary news: needs a path/title check


@dataclass
class SourceLists:
    factcheckers: dict[str, FactChecker] = field(default_factory=dict)  # host or domain -> entry
    domain_to_group: dict[str, str] = field(default_factory=dict)
    gov_domains: dict[str, str] = field(default_factory=dict)  # host -> independence group
    gov_suffixes: list[tuple[str, str]] = field(default_factory=list)  # (".gov.in", "govt_india")
    wire_groups: dict[str, str] = field(default_factory=dict)  # "pti" -> "pti" (byline token -> group)
    source_name_groups: dict[str, str] = field(default_factory=dict)  # slug(name) -> group


def _ifcn_reliability(kind: str, status: str) -> float:
    # Research note: rank Verified signatories first; non-signatories (CyberPeace, DFRAC) get a lower
    # weight; PIB Fact Check is authoritative for claims about the government but is a party to them.
    if kind == "govt_factcheck":
        return 0.85
    return {"Verified Signatory": 1.0, "In Renewal": 0.95, "Expired Signatory": 0.9}.get(status, 0.7)


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # a broken list must not take the evidence branch down
        log.warning("source_list_unreadable", path=str(path), error=str(exc))
        return {}


def slug(text: str) -> str:
    return re.sub(r"[^0-9a-zऀ-෿]+", "", text.lower())


@lru_cache(maxsize=1)
def load_lists(lists_dir: Path = LISTS_DIR) -> SourceLists:
    out = SourceLists()
    fc = _read(lists_dir / "factcheckers.json")
    for entry in fc.get("factcheckers") or []:
        domain = str(entry.get("domain") or "").lower()
        if not domain:
            continue
        kind = str(entry.get("kind") or "factchecker")
        status = str(entry.get("ifcn_status") or "not_listed")
        checker = FactChecker(
            domain=domain,
            name=str(entry.get("name") or domain),
            kind=kind,
            ifcn_status=status,
            reliability=_ifcn_reliability(kind, status),
            mixed=domain in _MIXED_OUTLETS,
        )
        for host in [domain, *(entry.get("other_domains") or [])]:
            out.factcheckers[str(host).lower()] = checker
    syn = _read(lists_dir / "syndication.json")
    out.domain_to_group = {str(k).lower(): str(v) for k, v in (syn.get("domain_to_group") or {}).items()}
    for key, wire in (syn.get("wires") or {}).items():
        out.wire_groups[str(key).lower()] = str(key).lower()
        for domain in wire.get("domains") or []:
            out.domain_to_group.setdefault(str(domain).lower(), str(key).lower())
    for key, group in (syn.get("ownership_groups") or {}).items():
        for domain in group.get("domains") or []:
            out.domain_to_group.setdefault(str(domain).lower(), str(key))
    for key, agg in (syn.get("aggregators") or {}).items():
        for domain in agg.get("domains") or []:
            out.domain_to_group.setdefault(str(domain).lower(), AGGREGATOR_PREFIX + str(key))
    gov = _read(lists_dir / "government_sources.json")
    for rule in gov.get("suffix_rules") or []:
        if rule.get("government_source") and rule.get("default_group"):
            out.gov_suffixes.append((str(rule["suffix"]).lower(), str(rule["default_group"])))
    for entry in gov.get("domains") or []:
        group = str(entry.get("independence_group") or GOVT_INDIA)
        for host in [entry.get("domain"), *(entry.get("aliases") or [])]:
            if host:
                out.gov_domains[str(host).lower()] = group
    log.debug(
        "source_lists_loaded",
        factcheckers=len(out.factcheckers),
        groups=len(out.domain_to_group),
        gov=len(out.gov_domains),
    )
    return out


def host_of(url_or_host: str) -> str:
    text = (url_or_host or "").strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    elif "/" in text:
        text = text.split("/", 1)[0]
    text = text.split(":", 1)[0].strip(".")
    return text[4:] if text.startswith("www.") else text


def registrable_domain(url_or_host: str) -> str:
    """'https://www.economictimes.indiatimes.com/x' -> 'indiatimes.com' (public-suffix aware)."""
    host = host_of(url_or_host)
    if not host:
        return ""
    ext = _EXTRACT(host)
    # tldextract >= 5.2 renamed registered_domain (deprecated); support both without the warning.
    top = (
        ext.top_domain_under_public_suffix
        if hasattr(ext, "top_domain_under_public_suffix")
        else ext.registered_domain
    )
    return (top or host).lower()


def _lookup(host: str, table: dict[str, str]) -> str | None:
    # Most specific first: "factcheck.afp.com" before "afp.com", "frontline.thehindu.com" before
    # "thehindu.com"; then walk up the labels.
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in table:
            return table[candidate]
    return None


def government_group(url_or_host: str) -> str | None:
    """Independence group when the host is a government source, else None."""
    lists = load_lists()
    host = host_of(url_or_host)
    if not host:
        return None
    group = _lookup(host, lists.gov_domains)
    if group:
        return group
    for suffix, default_group in lists.gov_suffixes:
        if host.endswith(suffix):
            return default_group
    return None


def is_government(url_or_host: str) -> bool:
    return government_group(url_or_host) is not None


def publisher_group(url_or_host: str) -> str:
    """Independence key: ownership group / wire / govt_india, else the registrable domain."""
    lists = load_lists()
    host = host_of(url_or_host)
    if not host:
        return "unknown"
    gov = government_group(host)
    if gov:
        return gov
    return _lookup(host, lists.domain_to_group) or registrable_domain(host)


def is_aggregator_group(group: str) -> bool:
    return group.startswith(AGGREGATOR_PREFIX)


def factchecker_for(url_or_host: str, *, url: str = "", title: str = "") -> FactChecker | None:
    """The fact-checker entry if this item is a fact-check article (mixed outlets need path/title)."""
    lists = load_lists()
    host = host_of(url_or_host)
    if not host:
        return None
    checker = None
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in lists.factcheckers:
            checker = lists.factcheckers[candidate]
            break
    if checker is None:
        return None
    if not checker.mixed:
        return checker
    path = urlsplit(url).path if "://" in (url or "") else ""
    if host.startswith("factcheck.") or _FACTCHECK_PATH.search(path) or _FACTCHECK_TITLE.search(title or ""):
        return checker
    return None


def looks_like_fact_check_title(title: str) -> bool:
    return bool(_FACTCHECK_TITLE.search(title or ""))
