"""Wikidata entity grounding + consistency checks (tool `wikidata`, keyless, WIKIDATA_USER_AGENT).

For each person named in the claim: `wbsearchentities` -> QID, then one SPARQL query for position held
(P39 with start P580 / end P582 qualifiers) and date of death (P570). Checks:
* the claim names an office ("Senator", "Chief Minister", ...) the person holds now -> consistent;
  the matching office(s) all ended before today -> contradicted;
* a quote attributed to a person who died earlier (and the claim does not mention the death) ->
  contradicted.
The outcome is carried as the stance of the Wikidata item (support = consistent, refute =
contradicted, neutral = grounded only); fusion turns it into the wikidata signal. Wikidata items are
never counted as publisher groups.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rapidfuzz import fuzz

from veriloop.contracts import RetrievalMethod, Stance
from veriloop.core.http import raise_for_status
from veriloop.deps import Deps
from veriloop.services.evidence.lexicon import mentions_death
from veriloop.services.evidence.models import Candidate, SourceOutcome
from veriloop.services.evidence.sources import tool_call

OFFICES = (
    "deputy chief minister",
    "chief minister",
    "deputy prime minister",
    "prime minister",
    "vice president",
    "president",
    "senator",
    "governor",
    "chief justice",
    "member of parliament",
    "member of the legislative assembly",
    "secretary of state",
    "speaker",
    "mayor",
    "chancellor",
    "minister",
    "mla",
    "mp",
)
_OFFICE_ALIASES = {"mp": "member of", "mla": "member of the", "senator": "senator"}


@dataclass
class Position:
    label: str
    start: datetime | None
    end: datetime | None


def claim_offices(text: str) -> list[str]:
    low = text.lower()
    found: list[str] = []
    for office in OFFICES:
        if re.search(rf"(?<![\w-]){re.escape(office)}(?![\w-])", low) and not any(office in f for f in found):
            found.append(office)
    return found


def _date(binding: dict[str, Any], name: str) -> datetime | None:
    value = (binding.get(name) or {}).get("value")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_facts(sparql: dict[str, Any]) -> tuple[list[Position], datetime | None]:
    positions: dict[tuple[str, datetime | None, datetime | None], Position] = {}
    death: datetime | None = None
    for b in ((sparql or {}).get("results") or {}).get("bindings") or []:
        death = death or _date(b, "dod")
        label = (b.get("posLabel") or {}).get("value")
        if label and not re.fullmatch(r"Q\d+", label):
            pos = Position(label, _date(b, "start"), _date(b, "end"))
            positions[(pos.label, pos.start, pos.end)] = pos
    return list(positions.values()), death


def _fmt(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else "?"


def check_consistency(
    claim_text: str,
    claim_type: str,
    positions: list[Position],
    death: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[Stance, str | None]:
    """(stance, the fact sentence that decided it). Neutral when no check applies."""
    now = now or datetime.now(UTC)
    if death is not None and claim_type == "quote" and not mentions_death(claim_text) and death < now:
        return Stance.REFUTE, f"Date of death: {_fmt(death)}."
    offices = claim_offices(claim_text)
    if offices and positions:
        matched = [
            p
            for p in positions
            if any(o in p.label.lower() or _OFFICE_ALIASES.get(o, "\0") in p.label.lower() for o in offices)
        ]
        current = [p for p in matched if p.end is None or p.end >= now]
        if current:
            p = current[0]
            return Stance.SUPPORT, f"Position held: {p.label} (since {_fmt(p.start)})."
        if matched:
            p = max(matched, key=lambda x: x.end or now)
            return Stance.REFUTE, f"Position held: {p.label} ({_fmt(p.start)} to {_fmt(p.end)})."
    return Stance.NEUTRAL, None


SPARQL = """SELECT ?dod ?posLabel ?start ?end WHERE {{
  OPTIONAL {{ wd:{qid} wdt:P570 ?dod . }}
  OPTIONAL {{ wd:{qid} p:P39 ?st . ?st ps:P39 ?pos .
             OPTIONAL {{ ?st pq:P580 ?start . }} OPTIONAL {{ ?st pq:P582 ?end . }} }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}} LIMIT 80"""


async def ground_person(
    deps: Deps, name: str, claim_text: str, claim_type: str, *, timeout_s: float = 5.0
) -> SourceOutcome:
    headers = {"User-Agent": deps.settings.wikidata_user_agent}
    params = {
        "action": "wbsearchentities",
        "search": name,
        "language": "en",
        "uselang": "en",
        "type": "item",
        "limit": "3",
        "format": "json",
    }

    async def search() -> Any:
        resp = await deps.tools.http.get("https://www.wikidata.org/w/api.php", params=params, headers=headers)
        raise_for_status(resp)  # core.http mapping: 429 -> rate_limited, 5xx retryable
        return resp.json()

    value, outcome = await tool_call(
        deps, "wikidata", "wbsearchentities", params, search, timeout_s=timeout_s, retries=1
    )
    if value is None:
        return outcome
    hits = [
        h
        for h in (value or {}).get("search") or []
        if fuzz.token_set_ratio(name.lower(), str(h.get("label") or "").lower()) >= 85
        and "disambiguation" not in str(h.get("description") or "").lower()
    ]
    if not hits:
        outcome.note = f"wikidata: no entity for {name!r}"
        return outcome
    hit = hits[0]
    qid, url = str(hit.get("id") or ""), hit.get("concepturi")
    if not re.fullmatch(r"Q\d+", qid) or not url:
        return outcome
    outcome.returned_urls.add(url)
    query = SPARQL.format(qid=qid)

    async def sparql() -> Any:
        resp = await deps.tools.http.get(
            "https://query.wikidata.org/sparql",
            params={"query": query, "format": "json"},
            headers={**headers, "Accept": "application/sparql-results+json"},
        )
        raise_for_status(resp)  # core.http mapping: 429 -> rate_limited, 5xx retryable
        return resp.json()

    facts, sp_outcome = await tool_call(
        deps, "wikidata", "sparql", {"qid": qid, "q": "p39_p570"}, sparql, timeout_s=timeout_s, retries=1
    )
    positions, death = parse_facts(facts) if facts else ([], None)
    stance, fact = check_consistency(claim_text, claim_type, positions, death)
    label = str(hit.get("label") or name)
    parts = [f"{label} ({qid}): {hit.get('description') or 'Wikidata entity'}."]
    if fact:
        parts.append(fact)
    for p in positions[:3]:
        line = f"Position held: {p.label} ({_fmt(p.start)} to {_fmt(p.end) if p.end else 'present'})."
        if line not in parts and (fact is None or p.label not in fact):
            parts.append(line)
    if death and (fact is None or "death" not in fact):
        parts.append(f"Date of death: {_fmt(death)}.")
    snippet = " ".join(parts)
    outcome.candidates.append(
        Candidate(
            source_name="Wikidata",
            url=url,
            host="wikidata.org",
            title=label,
            snippet=snippet,
            retrieval_method=RetrievalMethod.WIKIDATA_SPARQL,
            tool="wikidata",
            language="en",
            pinned=True,
            stance=stance,
            stance_confidence=0.9 if stance is not Stance.NEUTRAL else 0.0,
            stance_quote=fact,
            relevance=0.0,
        )
    )
    if sp_outcome.error is not None:
        outcome.error = sp_outcome.error  # grounded but facts unavailable: still a logged failure (G3)
    return outcome
