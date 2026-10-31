"""Seed the known-fake index (pHash + CLIP) from data/seed/known_fakes/manifest.csv.

    cd backend && $PY scripts/seed_known_fakes.py              # download missing media, (re)index all rows
    cd backend && $PY scripts/seed_known_fakes.py --only KF-001,KF-004 --no-clip

For every manifest row: download `media_url` (and image URLs the notes name as alternative copies)
into VERILOOP_HOME/seed/ through ToolClient (`page_fetch`), then upsert the base row plus variants:
the full image (sha256 + pHash + CLIP) and its centre crop (stamped fact-check thumbnails still match
the clean original). Idempotent: files already on disk are not re-downloaded and rows are upserts.
Failures are logged and skipped; nothing is invented for a row whose media could not be fetched (the
base row still carries the manifest's fact-check URLs so text matching keeps working).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import structlog

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from veriloop.core.cache import ToolCache  # noqa: E402
from veriloop.core.config import Settings, get_settings  # noqa: E402
from veriloop.core.executor import run_cpu  # noqa: E402
from veriloop.core.logging import configure_logging, register_secrets  # noqa: E402
from veriloop.core.tool_client import ToolClient  # noqa: E402
from veriloop.db import repo  # noqa: E402
from veriloop.db.engine import Database  # noqa: E402
from veriloop.services.ingestion.fetchers import download_image  # noqa: E402
from veriloop.services.ingestion.imaging import is_image_file  # noqa: E402
from veriloop.services.provenance import seeding  # noqa: E402

log = structlog.get_logger("seed_known_fakes")


def seed_dir(settings: Settings) -> Path:
    path = settings.home / "seed"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def fetch(tools: ToolClient, url: str, dest: Path) -> Path | None:
    """Reuse a previous download when it decodes; otherwise fetch through ToolClient."""
    if await run_cpu(lambda: dest.exists() and is_image_file(dest)):
        return dest
    res = await download_image(tools, url, dest)
    if not res.ok:
        log.warning("seed_download_failed", url=url, error=res.error.model_dump() if res.error else None)
        return None
    return dest if await run_cpu(is_image_file, dest) else None


async def seed_entry(
    db: Database, tools: ToolClient, settings: Settings, entry: seeding.ManifestEntry, *, use_clip: bool
) -> dict[str, object]:
    out: dict[str, object] = {"id": entry.id, "label": entry.label, "images": 0, "rows": 0, "failed": []}
    sources = [(entry.id, entry.media_url)] if entry.media_url else []
    sources += [(f"{entry.id}~alt{i + 1}", url) for i, url in enumerate(entry.alt_image_urls())]
    images: list[seeding.IndexedImage] = []
    for variant_id, url in sources:
        media_type = entry.media_type if url == entry.media_url else None
        name = variant_id.replace("~", "_") + seeding.extension_for(url, media_type)
        path = await fetch(tools, url, seed_dir(settings) / name)
        if path is None:
            out["failed"].append(url)  # type: ignore[union-attr]
            continue
        try:
            found = await run_cpu(
                seeding.image_variants_blocking, path, variant_id, settings, use_clip=use_clip
            )
        except ValueError as exc:
            log.warning("seed_image_undecodable", url=url, error=str(exc))
            out["failed"].append(url)  # type: ignore[union-attr]
            continue
        images.extend(found)
        out["images"] = int(out["images"]) + 1  # type: ignore[call-overload]
    out["rows"] = await seeding.upsert_entry(db, entry, images)
    out["with_clip"] = sum(1 for i in images if i.clip is not None)
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", type=Path, default=seeding.MANIFEST_PATH)
    ap.add_argument("--only", default="", help="comma-separated manifest ids")
    ap.add_argument("--no-clip", action="store_true", help="pHash only (skip the CLIP model)")
    args = ap.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    register_secrets(settings.secret_values())
    settings.ensure_dirs()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    entries = [e for e in seeding.read_manifest(args.manifest) if not only or e.id in only]

    started = time.perf_counter()
    db = await Database.connect(settings)
    tools = ToolClient(settings, cache=ToolCache())
    results: list[dict[str, object]] = []
    try:
        await db.apply_schema()
        for entry in entries:
            try:
                results.append(await seed_entry(db, tools, settings, entry, use_clip=not args.no_clip))
            except Exception as exc:  # one bad row never stops the seed
                log.exception("seed_entry_failed", id=entry.id, error=f"{type(exc).__name__}: {exc}")
                results.append({"id": entry.id, "error": f"{type(exc).__name__}: {exc}"})
        total = await repo.count_known_fakes(db)
    finally:
        await tools.aclose()
        await db.close()

    summary = {
        "manifest_entries": len(entries),
        "entries_with_media": sum(1 for r in results if int(r.get("images", 0) or 0) > 0),  # type: ignore[call-overload]
        "rows_upserted": sum(int(r.get("rows", 0) or 0) for r in results),  # type: ignore[call-overload]
        "known_fakes_rows_total": total,
        "seconds": round(time.perf_counter() - started, 1),
        "entries": results,
    }
    sys.stdout.write(json.dumps(summary, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
