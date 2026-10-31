"""Independence grouping (G4): which evidence items come from the same publisher group.

1. Domain -> group via data/lists/syndication.json (ownership groups, wires, aggregators) and
   government_sources.json (all central-government outlets = one group `govt_india`).
2. Wire copy: a dateline/byline token "(PTI)", "(ANI)", "(Reuters)" ... in the snippet collapses the
   item to the wire's group (two outlets running the same PTI story count once). Tokens in photo
   credits ("PTI Photo") are not bylines (syndication.json caution).
3. Aggregators (MSN, Yahoo, Dailyhunt): "<Publisher> on MSN" is attributed to that publisher when it
   is named, else the item keeps an `aggregator:*` group that fusion drops from counting.
4. Near-duplicate SNIPPETS: embedding cosine > 0.92 AND token Jaccard >= 0.6 between the article
   snippets collapse into one group (syndicated copy without a visible marker). Only items with a
   real body snippet take part: Google News / GDELT / PIB items carry only a headline, and every
   outlet reporting the same announcement writes nearly the same headline, so comparing headlines
   would wrongly merge independent publishers (e.g. PIB's release and The Hindu's report of it).
   A government source is never merged with a non-government one (a report about a release is
   a different publisher). The Jaccard guard keeps paraphrases apart (e5 scores them above 0.92).
"""

from __future__ import annotations

import re

import numpy as np

from veriloop.services.evidence.lists import (
    AGGREGATOR_PREFIX,
    is_aggregator_group,
    load_lists,
    publisher_group,
    registrable_domain,
    slug,
)
from veriloop.services.evidence.models import Candidate
from veriloop.services.evidence.text import jaccard

NEAR_DUP_COSINE = 0.92
NEAR_DUP_JACCARD = 0.60

_WIRE_TOKENS = {
    "pti": "pti",
    "ani": "ani",
    "ians": "ians",
    "uni": "uni",
    "reuters": "reuters",
    "ap": "ap",
    "afp": "afp",
}
_DATELINE = re.compile(r"\((PTI|ANI|IANS|UNI|Reuters|AP|AFP)\)(?!\s*Photo)", re.IGNORECASE)
_ON_AGGREGATOR = re.compile(
    r"^(?P<name>.+?)\s+(?:on|via)\s+(?:MSN|Yahoo(?:\s+News)?|Dailyhunt)$", re.IGNORECASE
)


def wire_from_dateline(text: str) -> str | None:
    m = _DATELINE.search(text or "")
    return _WIRE_TOKENS.get(m.group(1).lower()) if m else None


def base_group(cand: Candidate) -> str:
    group = publisher_group(cand.host or cand.url)
    wire = wire_from_dateline(cand.snippet)
    if wire and not cand.is_fact_check:
        return wire
    return group


def assign_groups(cands: list[Candidate]) -> None:
    """Set `domain` and `group` on every candidate (in place)."""
    lists = load_lists()
    by_name: dict[str, str] = {}
    for c in cands:
        c.domain = registrable_domain(c.host or c.url) or c.host
        c.group = base_group(c)
        if not is_aggregator_group(c.group):
            by_name.setdefault(slug(c.source_name), c.group)
    for c in cands:
        if not is_aggregator_group(c.group):
            continue
        m = _ON_AGGREGATOR.match(c.source_name.strip())
        if not m:
            continue
        name = slug(m.group("name"))
        # attribute to the named original publisher (same job, or a known group name), else keep it
        # as a distinct named publisher; only nameless aggregator pages stay uncounted.
        c.group = by_name.get(name) or lists.source_name_groups.get(name) or f"name:{name}"


_STRUCTURED = frozenset({"wikidata", "claimreview_feed", "factcheck"})
_MIN_BODY_CHARS = 40


def body_snippet(cand: Candidate) -> str:
    """The snippet text beyond the headline ('' when the tool returned only a headline)."""
    snippet = (cand.snippet or "").strip()
    title = (cand.title or "").strip()
    if snippet.lower().startswith(title.lower()):
        snippet = snippet[len(title) :].lstrip(" .:-")
    return snippet if len(snippet) >= _MIN_BODY_CHARS else ""


def collapse_near_duplicates(cands: list[Candidate]) -> int:
    """Union-find over headline near-duplicates; returns the number of merges."""
    n = len(cands)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    vecs = [c.embedding for c in cands]
    have = [
        i
        for i in range(n)
        if vecs[i] is not None
        and not cands[i].group.startswith(AGGREGATOR_PREFIX)
        and cands[i].tool not in _STRUCTURED  # structured records, not syndicated prose
        and body_snippet(cands[i])
    ]
    if len(have) < 2:
        return 0
    mat = np.stack([vecs[i] for i in have])
    sims = mat @ mat.T
    merges = 0
    for a in range(len(have)):
        for b in range(a + 1, len(have)):
            if sims[a, b] <= NEAR_DUP_COSINE:
                continue
            ca, cb = cands[have[a]], cands[have[b]]
            if ca.group == cb.group or ca.is_government != cb.is_government:
                continue
            if jaccard(body_snippet(ca), body_snippet(cb)) < NEAR_DUP_JACCARD:
                continue
            ra, rb = find(have[a]), find(have[b])
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
                merges += 1
    if not merges:
        return 0
    # Every component takes the group of its first (highest-ranked) member: deterministic.
    for i in range(n):
        root = find(i)
        if root != i:
            cands[i].group = cands[root].group
    return merges
