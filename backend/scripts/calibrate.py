"""Fit the Platt calibration of fusion's truth score T on data/eval/claims.jsonl and measure it (SPEC §5, D6).

    cd backend && $PY scripts/calibrate.py             # gather (cached) -> fit on train -> eval on test
    cd backend && $PY scripts/calibrate.py --refresh   # re-gather evidence (network, LLM stance calls)
    cd backend && $PY scripts/calibrate.py --no-write  # measure only, keep config/calibration.json

Extraction-free: each eval row becomes a one-claim ClaimExtraction (worthiness 1.0, the row's language,
no English rendering, no media), so the measurement isolates evidence + fusion. The real pipeline adds
claim extraction, forensics and provenance on top. NOT_CHECKABLE rows test the extraction gate and are
excluded here (reported as such).

Evidence per claim is cached on disk (VERILOOP_HOME/datasets/eval_cache/evidence/<id>.json) so reruns
are free; `--refresh` re-gathers. Platt scaling p = sigmoid(a*T + b) is fitted on the TRUE/FALSE rows
of the train split (Platt's smoothed targets + a small L2 prior, so the 20-odd points cannot push
`a` to infinity). Metrics on the test split: Brier, ECE (5 bins) on TRUE/FALSE rows; selective
precision / coverage / abstain rate on the full decide() output (2-group gate included); a sweep of
ABSTAIN_THRESHOLD chosen on train. Deepfake rows (label FALSE, notes "Deepfake") count MANIPULATED
as correct. Results go to config/calibration.json and the per-claim report next to the cache.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from veriloop.bootstrap import shutdown, startup  # noqa: E402
from veriloop.contracts import (  # noqa: E402
    AbstainBand,
    BranchResult,
    BranchStatus,
    Claim,
    ClaimExtraction,
    ForensicResult,
    ForensicStatus,
    ProvenanceResult,
    VerdictLabel,
)
from veriloop.core.config import REPO_ROOT, Settings, get_settings  # noqa: E402
from veriloop.core.logging import configure_logging  # noqa: E402
from veriloop.services.evidence import gather_evidence  # noqa: E402
from veriloop.services.evidence.lexicon import has_manipulation_words  # noqa: E402
from veriloop.services.fusion import decide, fuse, load_fusion_config  # noqa: E402
from veriloop.services.fusion.config import CALIBRATION_PATH, Calibration  # noqa: E402

log = structlog.get_logger("calibrate")

EVAL_PATH = REPO_ROOT / "data" / "eval" / "claims.jsonl"
THRESHOLDS = [round(0.50 + 0.05 * i, 2) for i in range(10)]  # 0.50 .. 0.95
DECISIVE = {VerdictLabel.TRUE, VerdictLabel.FALSE, VerdictLabel.MANIPULATED}
COST: dict[str, float] = {"inr": 0.0, "calls": 0}


def _capture_llm_cost(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    if event.get("event") == "llm_call":
        COST["calls"] += 1
        try:
            COST["inr"] += float(event.get("cost_inr") or 0.0)
        except (TypeError, ValueError):
            pass
    return event


def cache_dir(settings: Settings) -> Path:
    path = settings.home / "datasets" / "eval_cache"
    (path / "evidence").mkdir(parents=True, exist_ok=True)
    return path


def load_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in EVAL_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def extraction_for(row: dict[str, Any]) -> ClaimExtraction:
    return ClaimExtraction(
        language=row.get("language") or "en",
        claims=[Claim(id="C1", text=row["claim"])],
        primary_claim_id="C1",
        check_worthiness=1.0,
        worthiness_reason="calibration (extraction-free)",
    )


def no_forensic(settings: Settings) -> ForensicResult:
    band = AbstainBand(low=settings.forensic_low, high=settings.forensic_high, delta=settings.forensic_delta)
    return ForensicResult(branch_status=BranchStatus.SKIPPED, status=ForensicStatus.UNAVAILABLE, band=band)


def no_provenance() -> ProvenanceResult:
    return ProvenanceResult(branch_status=BranchStatus.SKIPPED)


async def gather_all(
    deps: Any, rows: list[dict[str, Any]], cdir: Path, *, refresh: bool, concurrency: int
) -> dict:
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, dict[str, Any]] = {}

    async def one(row: dict[str, Any]) -> None:
        path = cdir / "evidence" / f"{row['id']}.json"
        if path.exists() and not refresh:
            out[row["id"]] = json.loads(path.read_text(encoding="utf-8"))
            return
        async with sem:
            locale = "en-IN" if row.get("language", "en") == "en" else f"{row['language']}-IN"
            started = time.perf_counter()
            result = await gather_evidence(extraction_for(row), None, locale, deps)
            took = int((time.perf_counter() - started) * 1000)
        record = {
            "gathered_at": datetime.now(UTC).isoformat(),
            "wall_ms": took,
            "evidence": result.model_dump(mode="json"),
        }
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        out[row["id"]] = record
        log.info("eval_gathered", id=row["id"], items=len(result.items), status=result.status.value, ms=took)

    await asyncio.gather(*(one(r) for r in rows))
    return out


# ---- Platt fit ------------------------------------------------------------------------------------
def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def fit_platt(ts: list[float], ys: list[int], *, l2: float = 0.01, iters: int = 100) -> tuple[float, float]:
    """Newton's method on Platt's smoothed targets with an L2 prior on (a, b)."""
    n_pos, n_neg = sum(ys), len(ys) - sum(ys)
    t_pos, t_neg = (n_pos + 1) / (n_pos + 2), 1 / (n_neg + 2)
    targets = [t_pos if y else t_neg for y in ys]
    a, b = 1.0, 0.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for t, y in zip(ts, targets, strict=True):
            p = _sigmoid(a * t + b)
            w = p * (1 - p)
            ga += (p - y) * t
            gb += p - y
            haa += w * t * t
            hab += w * t
            hbb += w
        ga += l2 * a
        gb += l2 * b
        haa += l2
        hbb += l2
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-9:
            break
    return a, b


def brier(ps: list[float], ys: list[int]) -> float | None:
    return sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ys) if ys else None


def ece(ps: list[float], ys: list[int], bins: int = 5) -> float | None:
    """Expected calibration error of P(true) vs the TRUE label (equal-width bins)."""
    if not ys:
        return None
    total = 0.0
    for k in range(bins):
        lo, hi = k / bins, (k + 1) / bins
        idx = [i for i, p in enumerate(ps) if (lo <= p < hi) or (k == bins - 1 and p == 1.0)]
        if idx:
            conf = sum(ps[i] for i in idx) / len(idx)
            acc = sum(ys[i] for i in idx) / len(idx)
            total += len(idx) / len(ps) * abs(conf - acc)
    return total


# ---- evaluation -----------------------------------------------------------------------------------
@dataclass
class RowResult:
    id: str
    split: str
    gold: str
    deepfake: bool
    manipulated_media: bool
    truth_score: float
    p_true: float = 0.0
    label: str = ""
    reason: str | None = None
    groups_support: int = 0
    groups_refute: int = 0
    items: int = 0
    evidence_status: str = ""
    wall_ms: int = 0
    signals: dict[str, float | None] = field(default_factory=dict)

    @property
    def emitted(self) -> bool:
        return self.label in {v.value for v in DECISIVE}

    @property
    def correct_strict(self) -> bool:
        if self.label == "MANIPULATED" and self.gold == "FALSE":
            return self.deepfake
        return self.correct

    @property
    def correct(self) -> bool:
        if self.gold == "UNVERIFIABLE":
            return not self.emitted
        # README: deepfake rows are labelled FALSE and MANIPULATED counts as correct. The notes of the
        # AI-generated / digitally-altered rows describe manipulated media too (SPEC: MANIPULATED =
        # synthetic/deepfake findings); `correct_strict` keeps the literal "Deepfake"-only reading.
        if self.gold == "FALSE" and self.manipulated_media and self.label == "MANIPULATED":
            return True
        return self.label == self.gold


def evaluate(
    rows: list[dict[str, Any]], gathered: dict[str, dict[str, Any]], settings: Settings, cal: Calibration
) -> list[RowResult]:
    cfg = load_fusion_config()
    out = []
    for row in rows:
        ext = extraction_for(row)
        evidence = BranchResult.model_validate(gathered[row["id"]]["evidence"])
        fx, prov = no_forensic(settings), no_provenance()
        fusion = fuse(ext, evidence, fx, prov, settings, config=cfg, calibration=cal)
        verdict = decide(ext, fusion, None, fx, prov, evidence.items, settings, llm_enabled=False, config=cfg)
        out.append(
            RowResult(
                id=row["id"],
                split=row["split"],
                gold=row["label"],
                deepfake="deepfake" in (row.get("notes") or "").lower(),
                manipulated_media=has_manipulation_words(row.get("notes") or ""),
                truth_score=fusion.truth_score,
                p_true=fusion.p_true,
                label=verdict.label.value,
                reason=verdict.abstain_reason.value if verdict.abstain_reason else None,
                groups_support=fusion.groups_support,
                groups_refute=fusion.groups_refute,
                items=len(evidence.items),
                evidence_status=evidence.status.value,
                wall_ms=int(gathered[row["id"]].get("wall_ms") or 0),
                signals={s.name: (round(s.score, 3) if s.present else None) for s in fusion.signals},
            )
        )
    return out


def selective(results: list[RowResult]) -> dict[str, Any]:
    n = len(results)
    emitted = [r for r in results if r.emitted]
    correct_emitted = sum(r.correct for r in emitted)
    return {
        "n": n,
        "emitted": len(emitted),
        "coverage": round(len(emitted) / n, 4) if n else None,
        "abstain_rate": round(1 - len(emitted) / n, 4) if n else None,
        "selective_precision": round(correct_emitted / len(emitted), 4) if emitted else None,
        "selective_precision_strict_deepfake_only": (
            round(sum(r.correct_strict for r in emitted) / len(emitted), 4) if emitted else None
        ),
        "accuracy_incl_abstain": round(sum(r.correct for r in results) / n, 4) if n else None,
        "abstain_reasons": _count(r.reason for r in results if not r.emitted),
        "labels": _count(r.label for r in results),
    }


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def binary_metrics(results: list[RowResult]) -> dict[str, Any]:
    pairs = [(r.p_true, 1 if r.gold == "TRUE" else 0) for r in results if r.gold in ("TRUE", "FALSE")]
    ps, ys = [p for p, _ in pairs], [y for _, y in pairs]
    b, e = brier(ps, ys), ece(ps, ys)
    return {
        "n": len(ys),
        "brier": round(b, 4) if b is not None else None,
        "ece_5bin": round(e, 4) if e is not None else None,
    }


def with_threshold(settings: Settings, thr: float) -> Settings:
    return settings.model_copy(update={"abstain_threshold": thr})


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true", help="re-gather evidence (ignore the on-disk cache)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--no-write", action="store_true", help="do not overwrite config/calibration.json")
    ap.add_argument(
        "--target-precision", type=float, default=0.90, help="train precision the threshold must reach"
    )
    ap.add_argument("--only", default="", help="comma-separated row ids (debugging)")
    args = ap.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json_logs=False)
    rows = load_rows()
    if args.only:
        keep = set(args.only.split(","))
        rows = [r for r in rows if r["id"] in keep]
    excluded = [r["id"] for r in rows if r["label"] == "NOT_CHECKABLE"]
    rows = [r for r in rows if r["label"] != "NOT_CHECKABLE"]
    cdir = cache_dir(settings)

    started = time.perf_counter()
    deps = await startup(settings)
    # startup() reconfigures structlog, so the cost meter is installed after it.
    structlog.configure(processors=[_capture_llm_cost, *structlog.get_config()["processors"]])
    try:
        # Prewarm like ML_PREWARM does: a lazy NLI load mid-run (stance fallback) stalls the CPU pool.
        await deps.embedder.load()
        await deps.nli.load()
        gathered = await gather_all(deps, rows, cdir, refresh=args.refresh, concurrency=args.concurrency)
    finally:
        await shutdown(deps)
    gather_s = round(time.perf_counter() - started, 1)

    # 1) T per row is independent of the calibration: compute once with the identity-ish prior.
    prior = Calibration(version="prior", a=4.0, b=0.0)
    base = evaluate(rows, gathered, settings, prior)
    train = [r for r in base if r.split == "train" and r.gold in ("TRUE", "FALSE")]
    a, b = fit_platt([r.truth_score for r in train], [1 if r.gold == "TRUE" else 0 for r in train])
    version = f"platt-{datetime.now(UTC).date().isoformat()}-n{len(train)}"
    cal = Calibration(version=version, a=a, b=b, n=len(train))

    # 2) threshold sweep on train (choose), report on test
    sweep = []
    for thr in THRESHOLDS:
        res = evaluate(rows, gathered, with_threshold(settings, thr), cal)
        sweep.append(
            {
                "threshold": thr,
                "train": selective([r for r in res if r.split == "train"]),
                "test": selective([r for r in res if r.split == "test"]),
            }
        )
    ok = [s for s in sweep if (s["train"]["selective_precision"] or 0) >= args.target_precision]
    chosen = (
        min(ok, key=lambda s: s["threshold"])
        if ok
        else max(sweep, key=lambda s: (s["train"]["selective_precision"] or 0, -s["threshold"]))
    )
    thr = chosen["threshold"]
    final = evaluate(rows, gathered, with_threshold(settings, thr), cal)
    test = [r for r in final if r.split == "test"]
    trn = [r for r in final if r.split == "train"]
    prior_test = binary_metrics([r for r in base if r.split == "test"])

    metrics = {
        "test": {**selective(test), **{f"binary_{k}": v for k, v in binary_metrics(test).items()}},
        "train": {**selective(trn), **{f"binary_{k}": v for k, v in binary_metrics(trn).items()}},
        "test_prior_uncalibrated": {f"binary_{k}": v for k, v in prior_test.items()},
        "factly_only_false_rows_note": (
            "10 of 15 FALSE rows have one fact-check group (Factly) in the eval notes; the 2-group "
            "rule (G4) is expected to abstain on them unless retrieval finds a second group."
        ),
    }
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "gather_wall_s": gather_s,
        "llm_calls": COST["calls"],
        "llm_cost_inr": round(COST["inr"], 4),
        "excluded_not_checkable": excluded,
        "per_claim": [r.__dict__ | {"correct": r.correct, "emitted": r.emitted} for r in final],
        "sweep": sweep,
    }
    (cdir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    calibration = {
        "version": version,
        "method": "platt",
        "a": round(a, 6),
        "b": round(b, 6),
        "n": len(train),
        "fitted_on": "data/eval/claims.jsonl split=train, TRUE/FALSE rows (Platt smoothed targets, L2=0.01)",
        "input": "fusion truth score T (extraction-free evidence gathering; no media)",
        "weights_version": load_fusion_config().version,
        "created_at": report["created_at"],
        "recommended_abstain_threshold": thr,
        "threshold_rule": (
            f"lowest ABSTAIN_THRESHOLD whose TRAIN selective precision >= {args.target_precision:.2f}"
            + ("" if ok else " (none reached it: best train precision chosen)")
        ),
        "metrics": metrics,
        "eval_report": str(cdir / "report.json"),
    }
    if not args.no_write:
        CALIBRATION_PATH.write_text(
            json.dumps(calibration, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    summary = {k: calibration[k] for k in ("version", "a", "b", "n", "recommended_abstain_threshold")}
    summary |= {
        "metrics": metrics,
        "gather_wall_s": gather_s,
        "llm_calls": COST["calls"],
        "llm_cost_inr": round(COST["inr"], 4),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))  # CLI output (scripts may print)
    print(f"\nper-claim (threshold {thr:.2f}):")
    for r in final:
        print(
            f"  {r.id:6} {r.split:5} gold={r.gold:12} -> {r.label:12} {r.reason or '':22} "
            f"T={r.truth_score:+.2f} p={r.p_true:.2f} sup={r.groups_support} ref={r.groups_refute} "
            f"items={r.items:2} "
            f"{'OK' if r.correct else 'XX'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
