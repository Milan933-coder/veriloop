"""Known-fake index seeding: manifest parsing, image variants and upserts (used by the seed scripts).

Each manifest entry becomes one base row (`KF-004`) plus variant rows sharing the base id
(`KF-004~crop70`, `KF-004~alt1`, `KF-004~demo-b-f03`, see `index.base_id`). Variants exist because the
manifest images are mostly fact-check og:image thumbnails carrying a verdict stamp or border: the
centre crop matches the clean original, and frames sampled from the demo media match a re-upload of
the video itself. Everything here only copies values from the manifest or the downloaded files.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from veriloop.core.config import REPO_ROOT, Settings
from veriloop.db import repo
from veriloop.services.ingestion import ffmpeg
from veriloop.services.ingestion.imaging import (
    center_crop,
    is_informative,
    load_rgb,
    phash_hex,
    sha256_file,
)

from . import clip
from .index import VARIANT_SEP, format_origin

log = structlog.get_logger(__name__)

MANIFEST_PATH = REPO_ROOT / "data" / "seed" / "known_fakes" / "manifest.csv"
CROP_FRAC = 0.7  # middle 70 % of each side: drops verdict stamps/borders fact-checkers add
CROP_SUFFIX = "crop70"
# The DB (and the KnownFakeMatch contract) know three labels; AI-generated media is a manipulation.
LABELS = {
    "manipulated": "manipulated",
    "ai_generated": "manipulated",
    "out_of_context": "out_of_context",
    "authentic_reference": "authentic_reference",
}
_IMAGE_URL = re.compile(r"https?://\S+?\.(?:jpe?g|png|webp)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ManifestEntry:
    id: str
    label: str  # DB label (see LABELS)
    raw_label: str  # as written in the manifest
    claim: str
    media_url: str | None
    media_type: str | None
    source_page_url: str | None
    factcheck_urls: tuple[str, ...]
    publishers: tuple[str, ...]
    first_seen: datetime | None
    language: str | None
    notes: str

    @property
    def title(self) -> str:
        return self.claim if len(self.claim) <= 200 else self.claim[:197] + "..."

    @property
    def db_notes(self) -> str:
        prefix = f"Manifest label: {self.raw_label}. " if self.raw_label != self.label else ""
        return (prefix + self.notes).strip()

    def alt_image_urls(self) -> list[str]:
        """Other copies of the fake that the manifest notes name (image URLs only, never pages)."""
        urls = [u for u in _IMAGE_URL.findall(self.notes) if u != self.media_url]
        return list(dict.fromkeys(urls))


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (value or "").split("|") if p.strip())


def _date(value: str | None) -> datetime | None:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def read_manifest(path: str | Path = MANIFEST_PATH) -> list[ManifestEntry]:
    """Parse the seed manifest. Rows with an unknown label are skipped (logged), never guessed."""
    entries: list[ManifestEntry] = []
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("label") or "").strip()
            label = LABELS.get(raw)
            if label is None or not (row.get("id") or "").strip():
                log.warning("manifest_row_skipped", id=row.get("id"), label=raw)
                continue
            entries.append(
                ManifestEntry(
                    id=row["id"].strip(),
                    label=label,
                    raw_label=raw,
                    claim=(row.get("claim") or "").strip(),
                    media_url=(row.get("media_url") or "").strip() or None,
                    media_type=(row.get("media_type") or "").strip() or None,
                    source_page_url=(row.get("source_page_url") or "").strip() or None,
                    factcheck_urls=_split(row.get("factcheck_urls")),
                    publishers=_split(row.get("factcheck_publishers")),
                    first_seen=_date(row.get("first_seen_date")),
                    language=(row.get("language") or "").strip() or None,
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return entries


def extension_for(url: str | None, media_type: str | None) -> str:
    by_type = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    if media_type in by_type:
        return by_type[media_type]
    match = re.search(r"\.(jpe?g|png|webp|gif)(?:$|\?)", url or "", re.IGNORECASE)
    return f".{match.group(1).lower()}" if match else ".img"


@dataclass
class IndexedImage:
    variant_id: str
    sha256: str | None  # only for whole files: a crop's bytes never circulate
    phash: str
    clip: np.ndarray | None
    media_path: str


def image_variants_blocking(
    path: str | Path,
    variant_id: str,
    settings: Settings,
    *,
    crop: bool = True,
    use_clip: bool = True,
) -> list[IndexedImage]:
    """Full image (+ centre crop) -> sha256 / pHash / CLIP. Flat images return [] (they match anything)."""
    img = load_rgb(path)
    if not is_informative(img):
        return []
    images = [(variant_id, img, sha256_file(path))]
    if crop:
        images.append((f"{variant_id}{VARIANT_SEP}{CROP_SUFFIX}", center_crop(img, CROP_FRAC), None))
    vectors: np.ndarray | None = None
    if use_clip and settings.ml_enable:
        try:
            vectors = clip.embed_images_blocking([im for _, im, _ in images], settings)
        except RuntimeError as exc:
            log.warning("seed_clip_unavailable", error=str(exc))
    return [
        IndexedImage(
            variant_id=vid,
            sha256=sha,
            phash=phash_hex(im),
            clip=vectors[i] if vectors is not None else None,
            media_path=str(path),
        )
        for i, (vid, im, sha) in enumerate(images)
    ]


def sample_video_frames_blocking(video: str | Path, out_dir: str | Path, settings: Settings) -> list[Path]:
    """The exact frame plan ingestion uses (FRAME_FPS / FRAME_CAP), so a re-submission matches 1:1."""
    info = ffmpeg.probe_blocking(video)
    if not info.has_video:
        return []
    plan = ffmpeg.plan_frame_times(info.duration_s, settings.frame_fps, settings.frame_cap)
    frames = ffmpeg.extract_frames_blocking(video, plan.times, out_dir, duration_s=info.duration_s)
    return [p for _, p in frames]


async def upsert_entry(
    db: Any,
    entry: ManifestEntry,
    images: list[IndexedImage],
    *,
    origin: tuple[str, str | None] | None = None,
) -> int:
    """Upsert the base row (always) and one row per image variant. Returns rows written."""
    notes = entry.db_notes
    if origin is not None:
        notes = f"{notes} {format_origin(origin[0], origin[1])}".strip()
    written = 0
    if not any(img.variant_id == entry.id for img in images):
        # The base row carries the manifest metadata even when its own image could not be fetched.
        await repo.upsert_known_fake(
            db,
            id=entry.id,
            label=entry.label,
            source_urls=entry.factcheck_urls,
            title=entry.title,
            claim=entry.claim,
            notes=entry.db_notes,
            first_seen_at=entry.first_seen,
        )
        written += 1
    for img in images:
        await repo.upsert_known_fake(
            db,
            id=img.variant_id,
            label=entry.label,
            source_urls=entry.factcheck_urls,
            title=entry.title,
            claim=entry.claim,
            notes=notes if img.variant_id != entry.id else entry.db_notes,
            sha256=img.sha256,
            phash=img.phash,
            clip=img.clip,
            media_path=img.media_path,
            first_seen_at=entry.first_seen,
        )
        written += 1
    return written
