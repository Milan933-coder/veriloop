"""Measure the forensic ensemble on a small labelled face set (D6: report only what we measured).

    cd backend && $PY scripts/eval_forensics.py                  # sample + download (once), score, report
    cd backend && $PY scripts/eval_forensics.py --per-subset 10  # quicker, smaller run

Dataset: OpenRL/DeepFakeFace (HF hub, pinned revision; card license `openrail`, README body says
apache-2.0). Real = IMDB-WIKI celebrity photos (wiki.zip); fake = InsightFace face swaps (insight.zip),
Stable Diffusion inpainting (inpainting.zip) and SD v1.5 text-to-image faces (text2img.zip). Only the
sampled members are fetched, with HTTP range reads of the remote zips through ToolClient (`page_fetch`),
into VERILOOP_HOME/datasets/forensics (never the repo). The sample list is written to
data/eval/forensics/manifest.csv (ids only, no media).

Every image is scored by every local model (face models on margin face crops, the AI-generated
detector on the whole image) and then routed exactly like `run_forensics` (ensemble + abstain band +
E7 quality gate). JPEG-recompressed variants (q60, q30) measure E7. Model weights are the measured
AUC mapped to w = clip(2 * (AUC - 0.5), 0, 1); ensemble metrics use 2-fold cross-fitted weights (each
half scored with weights measured on the other half) so they are not fitted on their own test data.
Outputs: data/eval/forensics/results.json and backend/veriloop/services/forensic/weights.json.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import io
import json
import random
import re
import sys
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from veriloop.contracts import AbstainBand, ForensicStatus, ModelScore  # noqa: E402
from veriloop.core.cache import ToolCache  # noqa: E402
from veriloop.core.config import REPO_ROOT, Settings, get_settings  # noqa: E402
from veriloop.core.errors import ToolAttemptError  # noqa: E402
from veriloop.core.executor import run_cpu  # noqa: E402
from veriloop.core.logging import configure_logging, register_secrets  # noqa: E402
from veriloop.core.tool_client import ToolClient  # noqa: E402
from veriloop.services.forensic import faces, quality  # noqa: E402
from veriloop.services.forensic.classifiers import get_classifier  # noqa: E402
from veriloop.services.forensic.ensemble import decide  # noqa: E402
from veriloop.services.forensic.models_config import LOCAL_MODELS, WEIGHTS_FILE, auc_to_weight  # noqa: E402
from veriloop.services.forensic.service import prepare_frames_blocking  # noqa: E402
from veriloop.services.ingestion.imaging import load_rgb, sha256_file  # noqa: E402

log = structlog.get_logger("eval_forensics")

DATASET = "OpenRL/DeepFakeFace"
REVISION = "eb2a54fbf2567790bd3dc61d09fdaade6bb6c31c"
LICENSE = "openrail (dataset card metadata); README body states apache-2.0; real images from IMDB-WIKI"
ZIP_URL = "https://huggingface.co/datasets/{dataset}/resolve/{revision}/{name}.zip"
SUBSETS = {"wiki": "real", "insight": "fake", "inpainting": "fake", "text2img": "fake"}
SEED = 20261031
VARIANTS = {"orig": None, "q60": 60, "q30": 30}  # JPEG re-encode quality per variant (E7)
FULL_ON_FACES_MIN_AUC = 0.65  # the AI-image detector also votes on face frames only if it is this good
EVAL_DIR = REPO_ROOT / "data" / "eval" / "forensics"
_IMAGE = re.compile(r"\.(jpe?g|png)$", re.IGNORECASE)


# =============================================================================================
# remote zip members via ranged reads (each read is one ToolClient page_fetch call)
# =============================================================================================
class RangeFile(io.RawIOBase):
    """Seekable read-only view of a remote file; reads come in 256 KB-aligned blocks (few requests)."""

    BLOCK = 256 * 1024

    def __init__(self, fetch: Callable[[int, int], tuple[bytes, int]]) -> None:
        self._fetch = fetch
        self._pos = 0
        self._blocks: dict[int, bytes] = {}
        _, self._size = fetch(0, 0)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _block(self, index: int) -> bytes:
        if index not in self._blocks:
            start = index * self.BLOCK
            self._blocks[index], _ = self._fetch(start, min(self._size, start + self.BLOCK) - 1)
        return self._blocks[index]

    def read(self, size: int = -1) -> bytes:
        end = self._size if size is None or size < 0 else min(self._size, self._pos + size)
        if end <= self._pos:
            return b""
        if end - self._pos > 4 * self.BLOCK:  # a large member: one direct request, not cached
            data, _ = self._fetch(self._pos, end - 1)
        else:
            parts = []
            for index in range(self._pos // self.BLOCK, (end - 1) // self.BLOCK + 1):
                block = self._block(index)
                lo = max(self._pos, index * self.BLOCK) - index * self.BLOCK
                hi = min(end, (index + 1) * self.BLOCK) - index * self.BLOCK
                parts.append(block[lo:hi])
            data = b"".join(parts)
        self._pos += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def make_fetch(
    tools: ToolClient, url: str, loop: asyncio.AbstractEventLoop
) -> Callable[[int, int], tuple[bytes, int]]:
    """Blocking (start, end) -> (bytes, total size) used from the zipfile worker thread."""

    async def call(start: int, end: int) -> tuple[bytes, int]:
        async def fn() -> dict[str, Any]:
            resp = await tools.http.get(url, headers={"Range": f"bytes={start}-{end}"}, follow_redirects=True)
            if resp.status_code != 206:
                raise ToolAttemptError("http_error", f"expected 206 for a range read, got {resp.status_code}")
            total = int(resp.headers.get("content-range", "/0").rsplit("/", 1)[-1])
            return {"total": total, "data": base64.b64encode(resp.content).decode("ascii")}

        key = {"url": url, "start": start, "end": end}
        value = await tools.call_or_raise(
            "page_fetch", "dataset_range", key, fn, cache_ttl_s=0, timeout_s=120
        )
        return base64.b64decode(value["data"]), int(value["total"])

    return lambda start, end: asyncio.run_coroutine_threadsafe(call(start, end), loop).result()


@dataclass
class Sample:
    sample_id: str
    label: str  # real | fake
    subset: str
    member: str
    path: Path
    sha256: str = ""
    width: int = 0
    height: int = 0


def _download_subset_blocking(
    fetch: Callable[[int, int], tuple[bytes, int]], subset: str, n: int, out_dir: Path, exclude: set[str]
) -> list[Sample]:
    with zipfile.ZipFile(RangeFile(fetch)) as zf:
        names = sorted(i.filename for i in zf.infolist() if _IMAGE.search(i.filename) and i.file_size > 2048)
        rel = {name: name.split("/", 1)[-1] for name in names}
        pool = [name for name in names if rel[name] not in exclude]  # never the same face path twice
        chosen = sorted(random.Random(f"{SEED}-{subset}").sample(pool, n))
        samples: list[Sample] = []
        for member in chosen:
            flat = re.sub(r"[^A-Za-z0-9._-]+", "_", rel[member])
            path = out_dir / subset / flat
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(zf.read(member))
            samples.append(Sample(f"{subset}/{flat}", SUBSETS[subset], subset, member, path))
            exclude.add(rel[member])
        return samples


def _cached_samples(out_dir: Path, per_subset: int) -> list[Sample] | None:
    """The previous run's sample list when it matches this run's size and every file is still on disk."""
    index = out_dir / "samples.json"
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("revision") != REVISION or data.get("per_subset") != per_subset:
        return None
    samples = [Sample(**{**row, "path": out_dir / row["path"]}) for row in data.get("samples", [])]
    return samples if samples and all(s.path.exists() for s in samples) else None


async def build_dataset(tools: ToolClient, settings: Settings, per_subset: int) -> list[Sample]:
    out_dir = settings.home / "datasets" / "forensics"
    cached = await asyncio.to_thread(_cached_samples, out_dir, per_subset)
    if cached is not None:
        log.info("dataset_cached", n=len(cached))
        return cached
    loop = asyncio.get_running_loop()
    exclude: set[str] = set()
    samples: list[Sample] = []
    for subset, label in SUBSETS.items():
        n = per_subset * 3 if label == "real" else per_subset  # balanced: as many reals as fakes
        url = ZIP_URL.format(dataset=DATASET, revision=REVISION, name=subset)
        started = time.perf_counter()
        part = await asyncio.to_thread(
            _download_subset_blocking, make_fetch(tools, url, loop), subset, n, out_dir, exclude
        )
        log.info("subset_ready", subset=subset, n=len(part), seconds=round(time.perf_counter() - started, 1))
        samples.extend(part)
    for s in samples:
        s.sha256 = await run_cpu(sha256_file, s.path)
        img = await run_cpu(load_rgb, s.path)
        s.width, s.height = img.size
    rows = [{**s.__dict__, "path": s.path.relative_to(out_dir).as_posix()} for s in samples]
    index = {"dataset": DATASET, "revision": REVISION, "per_subset": per_subset, "samples": rows}
    (out_dir / "samples.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return samples


def write_manifest(samples: list[Sample]) -> Path:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    path = EVAL_DIR / "manifest.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "sample_id",
                "label",
                "subset",
                "dataset",
                "revision",
                "license",
                "zip_member",
                "sha256",
                "width",
                "height",
            ]
        )
        for s in samples:
            w.writerow(
                [
                    s.sample_id,
                    s.label,
                    s.subset,
                    DATASET,
                    REVISION,
                    LICENSE,
                    f"{s.subset}.zip:{s.member}",
                    s.sha256,
                    s.width,
                    s.height,
                ]
            )
    return path


# =============================================================================================
# scoring (same models, crops and routing as run_forensics)
# =============================================================================================
@dataclass
class Scored:
    sample: Sample
    variant: str
    faces: int
    usable_faces: int
    scores: dict[str, float | None] = field(default_factory=dict)  # model -> P(fake)
    quality_issue: str | None = None


def _variant_path(sample: Sample, variant: str, quality_level: int | None, root: Path) -> Path:
    if quality_level is None:
        return sample.path
    out = root / f"variants_{variant}" / sample.subset / (sample.path.stem + ".jpg")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        load_rgb(sample.path).save(out, format="JPEG", quality=quality_level)
    return out


def score_blocking(
    samples: list[Sample], variant: str, quality_level: int | None, settings: Settings
) -> list[Scored]:
    root = settings.home / "datasets" / "forensics"
    paths = [_variant_path(s, variant, quality_level, root) for s in samples]
    frames = prepare_frames_blocking(
        [(s.sample_id, str(p)) for s, p in zip(samples, paths, strict=True)], settings
    )
    by_id = {f.asset_id: f for f in frames}
    out: list[Scored] = []
    for sample, path in zip(samples, paths, strict=True):
        frame = by_id.get(sample.sample_id)
        scored = Scored(
            sample, variant, len(frame.faces) if frame else 0, len(frame.usable_faces) if frame else 0
        )
        if frame is not None:
            scored.quality_issue = quality.image_issue(path, frame.image.width, frame.image.height)
        out.append(scored)
    for spec in LOCAL_MODELS:
        clf = get_classifier(spec, settings)
        if spec.role == "face":
            owners: list[int] = []
            crops = []
            for i, sample in enumerate(samples):
                frame = by_id.get(sample.sample_id)
                for crop in frame.usable_faces if frame else []:
                    owners.append(i)
                    crops.append(crop.image)
            probs = clf.predict(crops)
            for i in range(len(samples)):
                mine = [p for o, p in zip(owners, probs, strict=True) if o == i]
                out[i].scores[spec.model_id] = max(mine) if mine else None  # most suspicious face
        else:
            images = [by_id[s.sample_id].image if s.sample_id in by_id else None for s in samples]
            idx = [i for i, im in enumerate(images) if im is not None]
            probs = clf.predict([images[i] for i in idx])
            for i in range(len(samples)):
                out[i].scores[spec.model_id] = None
            for i, p in zip(idx, probs, strict=True):
                out[i].scores[spec.model_id] = p
    return out


# =============================================================================================
# metrics
# =============================================================================================
def auc(labels: list[int], scores: list[float]) -> float | None:
    """ROC AUC via the Mann-Whitney U statistic (ties count half)."""
    pos = [s for y, s in zip(labels, scores, strict=True) if y == 1]
    neg = [s for y, s in zip(labels, scores, strict=True) if y == 0]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def model_metrics(rows: list[Scored], model_id: str, *, faces_only: bool = False) -> dict[str, Any]:
    pairs = [
        (1 if r.sample.label == "fake" else 0, r.scores[model_id])
        for r in rows
        if r.scores.get(model_id) is not None and (not faces_only or r.usable_faces)
    ]
    labels = [y for y, _ in pairs]
    scores = [float(s) for _, s in pairs]  # type: ignore[arg-type]
    correct = sum((s >= 0.5) == (y == 1) for y, s in zip(labels, scores, strict=True))
    by_subset = {}
    for subset, label in SUBSETS.items():
        if label == "fake":
            sub = [
                (1 if r.sample.label == "fake" else 0, float(r.scores[model_id]))  # type: ignore[arg-type]
                for r in rows
                if r.scores.get(model_id) is not None and r.sample.subset in ("wiki", subset)
            ]
            by_subset[f"real_vs_{subset}"] = auc([y for y, _ in sub], [s for _, s in sub])
    return {
        "n": len(pairs),
        "n_fake": sum(labels),
        "auc": auc(labels, scores),
        "accuracy_at_0.5": round(correct / len(pairs), 4) if pairs else None,
        "auc_by_fake_subset": by_subset,
    }


def weights_from(rows: list[Scored]) -> tuple[dict[str, float], dict[str, float | None], bool]:
    aucs: dict[str, float | None] = {}
    for spec in LOCAL_MODELS:
        aucs[spec.model_id] = model_metrics(rows, spec.model_id, faces_only=spec.role == "face")["auc"]
    weights = {m: auc_to_weight(a) for m, a in aucs.items()}
    full = next(s.model_id for s in LOCAL_MODELS if s.role == "full")
    full_on_faces = (model_metrics(rows, full, faces_only=True)["auc"] or 0.0) >= FULL_ON_FACES_MIN_AUC
    return weights, aucs, full_on_faces


def route(row: Scored, weights: dict[str, float], full_on_faces: bool) -> list[ModelScore]:
    """The models run_forensics would ask for this image (faces -> face models; else the full model)."""
    out: list[ModelScore] = []
    for spec in LOCAL_MODELS:
        applies = (spec.role == "face" and row.usable_faces > 0) or (
            spec.role == "full" and (row.usable_faces == 0 or full_on_faces)
        )
        score = row.scores.get(spec.model_id) if applies else None
        if applies and score is not None:
            out.append(
                ModelScore(
                    model_id=spec.model_id,
                    provider="local",
                    raw_score=score,
                    weight=weights[spec.model_id],
                    frames_scored=1,
                )
            )
    return out


def ensemble_metrics(rows: list[Scored], decisions: list[Any]) -> dict[str, Any]:
    n = len(rows)
    decided = [
        (r, d)
        for r, d in zip(rows, decisions, strict=True)
        if d.status in (ForensicStatus.REAL, ForensicStatus.MANIPULATED)
    ]
    correct = sum((d.status is ForensicStatus.MANIPULATED) == (r.sample.label == "fake") for r, d in decided)
    reals = [d for r, d in zip(rows, decisions, strict=True) if r.sample.label == "real"]
    fakes = [d for r, d in zip(rows, decisions, strict=True) if r.sample.label == "fake"]
    scored = [
        (1 if r.sample.label == "fake" else 0, d.ensemble_score)
        for r, d in zip(rows, decisions, strict=True)
        if d.ensemble_score is not None
    ]
    reasons: dict[str, int] = {}
    for d in decisions:
        for reason in d.reasons:
            key = re.sub(r"[\d.]+", "#", reason)
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "n": n,
        "auc_ensemble_score": auc([y for y, _ in scored], [s for _, s in scored]),
        "abstain_rate": round(1 - len(decided) / n, 4) if n else None,
        "coverage": round(len(decided) / n, 4) if n else None,
        "accuracy_on_decided": round(correct / len(decided), 4) if decided else None,
        "decided": len(decided),
        "false_manipulated_on_real": sum(d.status is ForensicStatus.MANIPULATED for d in reals),
        "missed_fake_called_real": sum(d.status is ForensicStatus.REAL for d in fakes),
        "status_counts": {s.value: sum(d.status is s for d in decisions) for s in ForensicStatus},
        "abstain_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
    }


def cross_fitted_decisions(rows: list[Scored], band: AbstainBand) -> list[Any]:
    """2 folds stratified by label: each half is decided with weights measured on the other half.

    Recompressed variants follow their source sample's fold, so no image informs its own weights.
    """
    samples = {r.sample.sample_id: r.sample.label for r in rows}
    ordered = sorted(samples, key=lambda s: (samples[s], random.Random(s).random()))
    fold = {sid: i % 2 for i, sid in enumerate(ordered)}
    params = {}
    for k in (0, 1):
        train = [r for r in rows if fold[r.sample.sample_id] != k and r.variant == "orig"]
        weights, _, full_on_faces = weights_from(train)
        params[k] = (weights, full_on_faces)
    out = []
    for r in rows:
        weights, full_on_faces = params[fold[r.sample.sample_id]]
        out.append(decide(route(r, weights, full_on_faces), band, quality_issue=r.quality_issue))
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-subset", type=int, default=20, help="fakes per fake subset (reals = 3x)")
    ap.add_argument("--no-weights", action="store_true", help="do not write the forensic weights.json")
    args = ap.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    register_secrets(settings.secret_values())
    settings.ensure_dirs()
    band = AbstainBand(low=settings.forensic_low, high=settings.forensic_high, delta=settings.forensic_delta)

    tools = ToolClient(settings, cache=ToolCache())
    timings: dict[str, float] = {}
    try:
        t = time.perf_counter()
        samples = await build_dataset(tools, settings, args.per_subset)
        timings["download_s"] = round(time.perf_counter() - t, 1)
        await faces.ensure_yunet(settings, tools)
    finally:
        await tools.aclose()
    manifest = write_manifest(samples)

    rows: list[Scored] = []
    for variant, q in VARIANTS.items():
        t = time.perf_counter()
        rows.extend(await run_cpu(score_blocking, samples, variant, q, settings))
        timings[f"score_{variant}_s"] = round(time.perf_counter() - t, 1)

    orig = [r for r in rows if r.variant == "orig"]
    weights, aucs, full_on_faces = weights_from(orig)
    decisions = cross_fitted_decisions(rows, band)
    results: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": {
            "id": DATASET,
            "revision": REVISION,
            "license": LICENSE,
            "manifest": str(manifest.relative_to(REPO_ROOT)).replace("\\", "/"),
        },
        "n_samples": len(samples),
        "class_counts": {lbl: sum(s.label == lbl for s in samples) for lbl in ("real", "fake")},
        "subset_counts": {sub: sum(s.subset == sub for s in samples) for sub in SUBSETS},
        "faces": {
            "detector": faces.backend(settings),
            "images_with_usable_face": sum(1 for r in orig if r.usable_faces),
            "images_without_usable_face": sum(1 for r in orig if not r.usable_faces),
        },
        "band": band.model_dump(),
        "models": {
            spec.model_id: {
                "revision": spec.revision,
                "role": spec.role,
                **{
                    v: model_metrics(
                        [r for r in rows if r.variant == v], spec.model_id, faces_only=spec.role == "face"
                    )
                    for v in VARIANTS
                },
            }
            for spec in LOCAL_MODELS
        },
        "full_model_on_face_images": model_metrics(
            orig, next(s.model_id for s in LOCAL_MODELS if s.role == "full"), faces_only=True
        ),
        "weights": weights,
        "weight_rule": "w = clip(2 * (AUC - 0.5), 0, 1), AUC on the original (non-recompressed) images",
        "full_model_on_face_frames": full_on_faces,
        "ensemble_cross_fitted": {
            v: ensemble_metrics(
                [r for r in rows if r.variant == v],
                [d for r, d in zip(rows, decisions, strict=True) if r.variant == v],
            )
            for v in VARIANTS
        },
        "e7_quality_gate_flagged": {
            v: sum(1 for r in rows if r.variant == v and r.quality_issue) for v in VARIANTS
        },
        "timings": timings,
        "per_sample": [
            {
                "sample_id": r.sample.sample_id,
                "label": r.sample.label,
                "variant": r.variant,
                "usable_faces": r.usable_faces,
                "quality_issue": r.quality_issue,
                "scores": {k: (round(v, 4) if v is not None else None) for k, v in r.scores.items()},
            }
            for r in rows
        ],
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if not args.no_weights:
        WEIGHTS_FILE.write_text(
            json.dumps(
                {
                    "source": "scripts/eval_forensics.py",
                    "dataset": f"{DATASET}@{REVISION}",
                    "generated_at": results["generated_at"],
                    "auc": aucs,
                    "weights": weights,
                    "full_model_on_face_frames": full_on_faces,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    summary = {
        k: results[k]
        for k in (
            "n_samples",
            "class_counts",
            "faces",
            "weights",
            "full_model_on_face_frames",
            "e7_quality_gate_flagged",
            "timings",
        )
    }
    summary["model_auc"] = {
        m: {v: results["models"][m][v]["auc"] for v in VARIANTS} for m in results["models"]
    }
    summary["ensemble"] = {
        v: {
            k: results["ensemble_cross_fitted"][v][k]
            for k in (
                "auc_ensemble_score",
                "abstain_rate",
                "accuracy_on_decided",
                "false_manipulated_on_real",
                "status_counts",
            )
        }
        for v in VARIANTS
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
