"""Test helpers for the evidence branch: a deterministic semantic embedder and feed builders."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Literal
from xml.sax.saxutils import escape

import numpy as np

from veriloop.services.evidence.text import tokens

DIM = 384
_COMMON = np.zeros(DIM, dtype=np.float32)
_COMMON[0] = 1.0


_STOP = frozenset("the and for with from that this into over are was were has have its".split())


def _bow(text: str) -> np.ndarray:
    vec = np.zeros(DIM, dtype=np.float32)
    for tok in tokens(text.lower()):  # the production tokenizer (keeps Indic words whole)
        if len(tok) < 3 or tok in _STOP:
            continue
        h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "big")
        vec[1 + h % (DIM - 1)] += 1.0
    n = np.linalg.norm(vec)
    if not n:  # no usable token: a text-specific direction, never the shared one (cos would be high)
        h = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
        vec[1 + h % (DIM - 1)] = 1.0
        n = 1.0
    return vec / n


class BowEmbedder:
    """Bag-of-words embedder whose cosines live in multilingual-e5's band: cos = 0.75 + 0.25 * bow_cos.

    Unrelated texts score ~0.75 (relevance 0 after rescaling), identical texts 1.0, and texts sharing
    most words ~0.9+, like the real model; deterministic and offline.
    """

    model_id = "bow-test"
    dim = DIM

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    @property
    def available(self) -> bool:
        return True

    def status(self) -> str:
        return "loaded"

    async def load(self) -> bool:
        return True

    async def embed(self, texts: Sequence[str], kind: Literal["query", "passage"] = "query") -> np.ndarray:
        self.calls.append((list(texts), kind))
        out = []
        for t in texts:
            v = np.sqrt(0.75) * _COMMON + np.sqrt(0.25) * _bow(t)
            out.append(v / np.linalg.norm(v))
        return np.stack(out).astype(np.float32) if out else np.zeros((0, DIM), dtype=np.float32)


def rfc822(dt: datetime | None = None) -> str:
    return format_datetime(dt or datetime(2026, 10, 28, 10, 0, tzinfo=UTC))


def gnews_rss(items: list[tuple[str, str, str, str]]) -> str:
    """items: (title, publisher name, publisher url, article id)."""
    body = "".join(
        f"<item><title>{escape(t)} - {escape(p)}</title>"
        f"<link>https://news.google.com/rss/articles/{gid}?oc=5</link>"
        f"<pubDate>{rfc822()}</pubDate>"
        f'<description>&lt;a href="x"&gt;{escape(t)}&lt;/a&gt;</description>'
        f'<source url="{escape(u)}">{escape(p)}</source></item>'
        for t, p, u, gid in items
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>{body}</channel></rss>'


def bing_rss(items: list[tuple[str, str, str, str]]) -> str:
    """items: (title, description, publisher url, source name)."""
    body = "".join(
        f"<item><title>{escape(t)}</title>"
        f"<link>https://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url={escape(u)}&amp;c=1</link>"
        f"<description>{escape(d)}</description><pubDate>{rfc822()}</pubDate>"
        f"<News:Source>{escape(s)}</News:Source></item>"
        for t, d, u, s in items
    )
    return (
        '<?xml version="1.0" encoding="utf-8" ?><rss version="2.0" '
        'xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss"><channel>'
        f"{body}</channel></rss>"
    )
