"""Own near-duplicate index: sha256 / pHash (Hamming) / CLIP (cosine) over known fakes + prior runs.

Known-fake rows can have *variants* sharing a base id (`KF-004~crop70`, `KF-004~demo-f03`): the seed
script indexes a centre crop of every fact-check thumbnail (stamps/borders removed) and the frames of
demo media. Matches are merged back to the base id. A variant's `notes` may carry the original
appearance as `[origin: <url> @ <YYYY-MM-DD>]`, which feeds the first-seen estimate.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from veriloop.contracts import KnownFakeMatch, MediaAsset
from veriloop.db import repo
from veriloop.db.repo import KnownFake

PHASH_MAX_DISTANCE = 10
CLIP_MIN_SIMILARITY = 0.93
VARIANT_SEP = "~"
_ORIGIN = re.compile(r"\[origin:\s*(\S+?)\s*(?:@\s*(\d{4}-\d{2}-\d{2}))?\s*\]")


def base_id(fake_id: str) -> str:
    return fake_id.split(VARIANT_SEP, 1)[0]


def format_origin(url: str, date: str | None) -> str:
    return f"[origin: {url}{f' @ {date}' if date else ''}]"


def parse_origin(notes: str | None) -> tuple[str | None, datetime | None]:
    match = _ORIGIN.search(notes or "")
    if not match:
        return None, None
    when = None
    if match.group(2):
        try:
            when = datetime.strptime(match.group(2), "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            when = None
    return match.group(1), when


def strip_origin(notes: str | None) -> str | None:
    cleaned = _ORIGIN.sub("", notes or "").strip()
    return cleaned or None


@dataclass
class FakeHit:
    base: str
    rows: dict[str, KnownFake] = field(default_factory=dict)  # variant id -> row
    hamming: int | None = None
    clip: float | None = None
    assets: set[str] = field(default_factory=set)
    via: set[str] = field(default_factory=set)  # sha256 | phash | clip

    def add(
        self, fake: KnownFake, asset_id: str, *, hamming: int | None, clip: float | None, via: str
    ) -> None:
        self.rows[fake.id] = fake
        self.assets.add(asset_id)
        self.via.add(via)
        if hamming is not None:
            self.hamming = hamming if self.hamming is None else min(self.hamming, hamming)
        if clip is not None:
            self.clip = clip if self.clip is None else max(self.clip, clip)

    @property
    def primary(self) -> KnownFake:
        return self.rows.get(self.base) or next(iter(self.rows.values()))

    def origins(self) -> list[tuple[str, datetime | None]]:
        out: list[tuple[str, datetime | None]] = []
        for row in self.rows.values():
            url, when = parse_origin(row.notes)
            if url and (url, when) not in out:
                out.append((url, when or row.first_seen_at))
        return out

    def to_match(self) -> KnownFakeMatch:
        fake = self.primary
        return KnownFakeMatch(
            known_fake_id=self.base,
            label=fake.label,
            hamming=self.hamming,
            clip_similarity=round(self.clip, 4) if self.clip is not None else None,
            source_urls=list(fake.source_urls),
            title=fake.title,
            notes=strip_origin(fake.notes),
        )


@dataclass
class PriorHit:
    job_id: str
    assets: set[str] = field(default_factory=set)
    hamming: int | None = None
    clip: float | None = None


@dataclass
class IndexResult:
    fakes: dict[str, FakeHit] = field(default_factory=dict)
    prior: dict[str, PriorHit] = field(default_factory=dict)


async def search(
    db: Any, job_id: str, assets: list[MediaAsset], vectors: dict[str, np.ndarray]
) -> IndexResult:
    """Cheapest first per asset: exact sha256, then pHash Hamming <= 10, then CLIP cosine >= 0.93."""
    result = IndexResult()

    async def one(asset: MediaAsset) -> None:
        fake_sha = await repo.find_known_fakes_by_sha256(db, asset.sha256)
        for fake in fake_sha:
            _fake(result, fake, asset.id, hamming=0, clip=None, via="sha256")
        if asset.phash:
            for hit in await repo.search_known_fakes_by_phash(
                db, asset.phash, max_distance=PHASH_MAX_DISTANCE
            ):
                _fake(result, hit.fake, asset.id, hamming=hit.hamming, clip=None, via="phash")
            for prior in await repo.search_media_by_phash(
                db, asset.phash, max_distance=PHASH_MAX_DISTANCE, exclude_job_id=job_id
            ):
                _prior(result, prior.job_id, asset.id, hamming=prior.hamming, clip=None)
        vec = vectors.get(asset.id)
        if vec is not None:
            for hit in await repo.search_known_fakes_by_clip(
                db, vec, limit=5, min_similarity=CLIP_MIN_SIMILARITY
            ):
                _fake(result, hit.fake, asset.id, hamming=None, clip=hit.clip_similarity, via="clip")
            for prior in await repo.search_media_by_clip(
                db, vec, limit=5, min_similarity=CLIP_MIN_SIMILARITY, exclude_job_id=job_id
            ):
                _prior(result, prior.job_id, asset.id, hamming=None, clip=prior.similarity)

    await asyncio.gather(*(one(a) for a in assets))
    return result


def _fake(
    result: IndexResult, fake: KnownFake, asset_id: str, *, hamming: int | None, clip: float | None, via: str
) -> None:
    key = base_id(fake.id)
    result.fakes.setdefault(key, FakeHit(base=key)).add(fake, asset_id, hamming=hamming, clip=clip, via=via)


def _prior(
    result: IndexResult, job_id: str, asset_id: str, *, hamming: int | None, clip: float | None
) -> None:
    hit = result.prior.setdefault(job_id, PriorHit(job_id=job_id))
    hit.assets.add(asset_id)
    if hamming is not None:
        hit.hamming = hamming if hit.hamming is None else min(hit.hamming, hamming)
    if clip is not None:
        hit.clip = clip if hit.clip is None else max(hit.clip, clip)
