"""Metadata signals (BONUS only, SPEC §4): EXIF, container/re-encode hints, C2PA Content Credentials.

None of these can make media "manipulated" on their own. Platforms strip EXIF and re-encode every
upload, so absence of metadata is normal; the C2PA note is always included verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from PIL import Image

from veriloop.contracts import MediaAsset, MediaKind, MetadataReport, ResolvedInput
from veriloop.services.ingestion import ffmpeg

from .quality import jpeg_quality

log = structlog.get_logger(__name__)

_EDITORS = (
    "photoshop",
    "gimp",
    "snapseed",
    "lightroom",
    "faceapp",
    "remini",
    "picsart",
    "canva",
    "facetune",
    "midjourney",
    "dall",
    "stable diffusion",
    "firefly",
)
_EXIF_SOFTWARE, _EXIF_MAKE, _EXIF_MODEL = 0x0131, 0x010F, 0x0110


def _exif(path: str | Path) -> dict[str, Any]:
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            fmt = im.format
    except OSError:
        return {}
    return {
        "present": len(exif) > 0,
        "software": str(exif.get(_EXIF_SOFTWARE) or "").strip() or None,
        "camera": " ".join(str(exif.get(t) or "").strip() for t in (_EXIF_MAKE, _EXIF_MODEL)).strip() or None,
        "format": fmt,
    }


def _c2pa(path: str | Path) -> dict[str, Any]:
    try:
        import c2pa
    except ImportError:
        return {"error": "c2pa-python not installed"}
    try:
        reader = c2pa.Reader.try_create(str(path))
    except Exception as exc:  # unsupported container or corrupt manifest: report, never guess
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if reader is None:
        return {"present": False}
    try:
        store = json.loads(reader.json())
    finally:
        try:
            reader.close()
        except Exception:
            pass
    active = (store.get("manifests") or {}).get(store.get("active_manifest") or "") or {}
    sig = active.get("signature_info") or {}
    actions: list[str] = []
    for assertion in active.get("assertions") or []:
        if str(assertion.get("label", "")).startswith("c2pa.actions"):
            for action in (assertion.get("data") or {}).get("actions") or []:
                if action.get("action"):
                    actions.append(str(action["action"]))
    return {
        "present": True,
        "signer": sig.get("common_name") or active.get("claim_generator"),
        "issuer": sig.get("issuer"),
        "actions": actions,
    }


def analyze_blocking(resolved: ResolvedInput) -> MetadataReport:
    report = MetadataReport()
    hints: list[str] = []
    originals: list[str] = []
    stills = [a for a in resolved.media if a.kind is MediaKind.IMAGE and a.source in ("upload", "download")]
    for asset in stills[:2]:
        originals.append(asset.path)
        _image_hints(asset, report, hints)
    if resolved.video_path:
        originals.append(resolved.video_path)
        _video_hints(resolved.video_path, report, hints)
    for path in originals:
        info = _c2pa(path)
        if info.get("present"):
            report.c2pa_present = True
            report.c2pa_signer = info.get("signer")
            report.c2pa_issuer = info.get("issuer")
            report.c2pa_actions = list(dict.fromkeys(info.get("actions") or []))
            break
        if info.get("error"):
            log.info("c2pa_read_error", path=Path(path).name, error=info["error"])
    report.reencode_hints = list(dict.fromkeys(hints))
    return report


def _image_hints(asset: MediaAsset, report: MetadataReport, hints: list[str]) -> None:
    info = _exif(asset.path)
    if not info:
        return
    report.container = report.container or (info.get("format") or "").lower() or None
    if info.get("present"):
        report.exif_present = True
        report.exif_software = report.exif_software or info.get("software")
        software = (info.get("software") or "").lower()
        if any(tool in software for tool in _EDITORS):
            hints.append(f"EXIF Software tag names an editing/generation tool: {info.get('software')}")
    elif asset.source == "upload" and info.get("format") == "JPEG":
        # Cameras always write EXIF; its absence on an uploaded JPEG means it passed through an app
        # or platform that strips it (very common, hence only "suspected" and bonus-only).
        report.exif_stripped_suspected = True
    q = jpeg_quality(asset.path)
    if q is not None and q < 85:
        hints.append(f"JPEG quality ~{q}: recompressed at least once")


def _video_hints(path: str, report: MetadataReport, hints: list[str]) -> None:
    try:
        info = ffmpeg.probe_blocking(path)
    except ffmpeg.MediaToolError as exc:
        log.info("metadata_probe_failed", error=str(exc))
        return
    report.container = info.format_name
    encoder = info.tags.get("encoder") or info.video_tags.get("encoder")
    if encoder and encoder.lower().startswith("lavf"):
        hints.append(f"container written by FFmpeg ({encoder}): remuxed/re-encoded after capture")
    elif encoder:
        hints.append(f"encoder tag: {encoder}")
    handler = info.video_tags.get("handler_name")
    if handler:
        hints.append(f"video handler: {handler}")
    if "creation_time" not in info.tags and "creation_time" not in info.video_tags:
        hints.append("no container creation_time (typical after platform re-encoding)")
