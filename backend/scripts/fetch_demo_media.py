"""Download the demo-case media (docs/demo_cases.md) into VERILOOP_HOME/demo and seed it as known fakes.

    cd backend && $PY scripts/fetch_demo_media.py            # download (if missing) + seed frames
    cd backend && $PY scripts/fetch_demo_media.py --no-seed  # download only

Case (b): the original Typhoon Bavi YouTube short (falsely shared as Cyclone Arnab) -> KF-004.
Case (c): PIB Fact Check's copy of the Blumenthal deepfake on X -> KF-001.
Videos go through yt-dlp (ToolClient tool `ytdlp`, same caps as ingestion); still frames named in
demo_cases.md through `page_fetch`. Frames are sampled with ingestion's exact plan (FRAME_FPS /
FRAME_CAP), so submitting the same video in the live demo matches at pHash distance 0. Each frame
variant carries `[origin: <url> @ <upload date>]` from the yt-dlp metadata (first-seen estimate).
If X needs a login, the case falls back to its stills and the failure is printed, never hidden.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from veriloop.services.ingestion import ytdlp  # noqa: E402
from veriloop.services.ingestion.fetchers import download_image  # noqa: E402
from veriloop.services.ingestion.imaging import is_image_file  # noqa: E402
from veriloop.services.ingestion.pagemeta import parse_date  # noqa: E402
from veriloop.services.provenance import seeding  # noqa: E402

log = structlog.get_logger("fetch_demo_media")


@dataclass(frozen=True)
class DemoCase:
    case: str
    known_fake_id: str
    video_url: str
    stills: tuple[str, ...]  # still images of the same fake (docs/demo_cases.md)


CASES = (
    DemoCase(
        "b",
        "KF-004",
        "https://www.youtube.com/shorts/XeYAGU1Z6l8",
        ("https://english.factcrescendo.com/wp-content/uploads/2026/10/Image-4-1-fact-checked.webp",),
    ),
    DemoCase(
        "c",
        "KF-001",
        "https://x.com/PIBFactCheck/status/2112600705232249021",
        (
            "https://www.boomlive.in/h-upload/2026/10/22/1058741-collapse-economy-and-military.jpg",
            "https://factly.in/wp-content/uploads//2026/10/US-Senator-Thumbnail-.jpg",
        ),
    ),
)


def case_dir(settings: Settings, case: str) -> Path:
    path = settings.home / "demo" / f"case_{case}"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def fetch_video(tools: ToolClient, settings: Settings, demo: DemoCase) -> dict[str, Any]:
    out_dir = case_dir(settings, demo.case)
    info_file = out_dir / "info.json"
    existing = sorted(p for p in out_dir.glob("source.*") if p.suffix not in {".part", ".ytdl"})
    if existing and info_file.exists():
        return {
            "path": str(existing[0]),
            "info": json.loads(info_file.read_text(encoding="utf-8")),
            "cached": True,
        }
    max_bytes = settings.max_upload_mb * 2**20
    key = {
        "url": demo.video_url,
        "max_mb": settings.max_upload_mb,
        "max_video_s": settings.max_video_s,
        "res": 720,
    }
    res = await tools.call(
        "ytdlp",
        "download",
        key,
        lambda: ytdlp.download(
            demo.video_url, out_dir, max_bytes=max_bytes, max_video_s=settings.max_video_s, max_res=720
        ),
    )
    if not res.ok:
        assert res.error is not None
        return {"error": f"{res.error.error_type}: {res.error.message}"}
    value = res.value or {}
    info_file.write_text(json.dumps(value.get("info") or {}, indent=2), encoding="utf-8")
    return {"path": value.get("path"), "info": value.get("info") or {}, "cached": False}


async def fetch_stills(tools: ToolClient, settings: Settings, demo: DemoCase) -> list[Path]:
    paths: list[Path] = []
    for i, url in enumerate(demo.stills, start=1):
        dest = case_dir(settings, demo.case) / f"still_{i}{seeding.extension_for(url, None)}"
        if not await run_cpu(lambda d=dest: d.exists() and is_image_file(d)):
            res = await download_image(tools, url, dest)
            if not res.ok:
                log.warning("demo_still_failed", url=url, error=res.error.message if res.error else None)
                continue
        paths.append(dest)
    return paths


async def seed_case(
    db: Database,
    settings: Settings,
    demo: DemoCase,
    entry: seeding.ManifestEntry,
    video: dict[str, Any],
    stills: list[Path],
) -> dict[str, Any]:
    images: list[seeding.IndexedImage] = []
    frames: list[Path] = []
    origin: tuple[str, str | None] | None = None
    if video.get("path"):
        frames = await run_cpu(
            seeding.sample_video_frames_blocking,
            video["path"],
            case_dir(settings, demo.case) / "frames",
            settings,
        )
        for i, frame in enumerate(frames, start=1):
            variant = f"{entry.id}~demo-{demo.case}-f{i:02d}"
            images.extend(
                await run_cpu(seeding.image_variants_blocking, frame, variant, settings, crop=False)
            )
        info = video.get("info") or {}
        uploaded = parse_date(info.get("timestamp") or info.get("upload_date"))
        origin = (
            str(info.get("webpage_url") or demo.video_url),
            uploaded.strftime("%Y-%m-%d") if uploaded else None,
        )
    rows = await seeding.upsert_entry(db, entry, images, origin=origin) if images else 0
    still_images: list[seeding.IndexedImage] = []
    for i, still in enumerate(stills, start=1):
        still_images.extend(
            await run_cpu(
                seeding.image_variants_blocking, still, f"{entry.id}~demo-{demo.case}-still{i}", settings
            )
        )
    if still_images:
        rows += await seeding.upsert_entry(db, entry, still_images)
    return {
        "frames_sampled": len(frames),
        "frame_rows": len(images),
        "still_rows": len(still_images),
        "rows_upserted": rows,
        "origin": origin,
    }


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-seed", action="store_true")
    args = ap.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    register_secrets(settings.secret_values())
    settings.ensure_dirs()
    manifest = {e.id: e for e in seeding.read_manifest()}

    tools = ToolClient(settings, cache=ToolCache())
    db = None if args.no_seed else await Database.connect(settings)
    report: dict[str, Any] = {}
    try:
        if db is not None:
            await db.apply_schema()
        for demo in CASES:
            started = time.perf_counter()
            video = await fetch_video(tools, settings, demo)
            stills = await fetch_stills(tools, settings, demo)
            entry: dict[str, Any] = {
                "video_url": demo.video_url,
                "video": {k: v for k, v in video.items() if k != "info"},
                "title": (video.get("info") or {}).get("title"),
                "duration_s": (video.get("info") or {}).get("duration"),
                "upload_date": (video.get("info") or {}).get("upload_date"),
                "stills": [str(p) for p in stills],
            }
            if video.get("path"):
                # A stable, human-friendly copy for the presenter's upload dialog.
                friendly = (
                    case_dir(settings, demo.case) / f"demo_case_{demo.case}{Path(video['path']).suffix}"
                )
                if not friendly.exists():
                    await asyncio.to_thread(shutil.copy2, video["path"], friendly)
                entry["upload_file"] = str(friendly)
            if db is not None:
                entry["seed"] = await seed_case(
                    db, settings, demo, manifest[demo.known_fake_id], video, stills
                )
            entry["seconds"] = round(time.perf_counter() - started, 1)
            report[f"case_{demo.case}"] = entry
        if db is not None:
            report["known_fakes_rows_total"] = await repo.count_known_fakes(db)
    finally:
        await tools.aclose()
        if db is not None:
            await db.close()
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
