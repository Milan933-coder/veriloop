"""Run the scripted demo cases (docs/demo_cases.md) end to end through the HTTP API and save a summary.

    # terminal 1: a server that records every tool response as a replay cassette (D-12)
    TOOL_MODE=record python -m veriloop            (PowerShell: $env:TOOL_MODE="record"; python -m veriloop)
    # terminal 2
    cd backend
    python scripts/record_demo.py                        # cases a b c d, uploads for b/c when the clips exist
    python scripts/record_demo.py --cases b --text-only  # one case, caption text only
    # later, offline: TOOL_MODE=replay python -m veriloop  -> same answers from data/fixtures/cassettes

For each case: POST /verify (JSON, or multipart with the clip from VERILOOP_HOME/demo/case_<x>/),
follow the SSE stream to the end (prints the node timeline), GET the JobView, check it against the
expected label, GET /audit/{id}/verify, and for report-eligible verdicts POST /report/{id} (prepare
only). It NEVER confirms a report or generates a legal draft: those stay behind the human gate (G5).
The summary (job ids, verdicts, timings, audit status) goes to VERILOOP_HOME/demo/runs/.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from veriloop.core.config import get_settings


@dataclass(frozen=True)
class DemoCase:
    key: str
    title: str
    text: str
    expected: str
    media: str | None = None  # file under VERILOOP_HOME/demo/case_<key>/


CASES: dict[str, DemoCase] = {
    "a": DemoCase(
        "a",
        "TRUE: Cabinet approves 3% DA hike",
        "The Union Cabinet approved a 3% hike in Dearness Allowance for central government employees "
        "and pensioners on 28 October 2026.",
        "TRUE",
    ),
    "b": DemoCase(
        "b",
        "FALSE viral: Cyclone Arnab video",
        "This video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh.",
        "FALSE",
        "demo_case_b.mp4",
    ),
    "c": DemoCase(
        "c",
        "DEEPFAKE: Senator Blumenthal video",
        "Video: US Senator Richard Blumenthal says India is a failed state with a collapsing "
        "economy and military.",
        "MANIPULATED",
        "demo_case_c.mp4",
    ),
    "d": DemoCase(
        "d",
        "AMBIGUOUS: 8th Pay Commission fitment factor",
        "The 8th Pay Commission will recommend a fitment factor of 2.86 for central government employees.",
        "UNVERIFIABLE",
    ),
}


def submit(client: httpx.Client, case: DemoCase, media: Path | None) -> str:
    if media is not None:
        mime = mimetypes.guess_type(media.name)[0] or "application/octet-stream"
        with media.open("rb") as fh:
            resp = client.post(
                "/verify",
                files={"file": (media.name, fh, mime)},
                data={"text": case.text, "platform": "upload", "locale": "en-IN", "source": "api"},
            )
    else:
        resp = client.post("/verify", json={"text": case.text, "locale": "en-IN", "source": "api"})
    if resp.status_code != 202:
        raise RuntimeError(f"POST /verify -> {resp.status_code}: {resp.text[:300]}")
    return str(resp.json()["job_id"])


def follow_events(client: httpx.Client, job_id: str, timeout_s: float) -> list[dict[str, Any]]:
    """Read the SSE stream until the final {"type":"done"} event; print node ends as they arrive."""
    events: list[dict[str, Any]] = []
    with client.stream(
        "GET", f"/verify/{job_id}/events", timeout=httpx.Timeout(timeout_s, read=timeout_s)
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            events.append(event)
            if event.get("type") == "node" and event.get("status") in ("end", "error"):
                detail = f"  {event.get('detail')}" if event.get("detail") else ""
                ms = event.get("duration_ms", 0)
                print(f"    {event['node']:<18} {event['status']:<5} {ms:>6} ms{detail[:90]}")
            if event.get("type") == "done":
                break
    return events


def run_case(client: httpx.Client, case: DemoCase, *, home: Path, text_only: bool, timeout_s: float) -> dict:
    media = home / "demo" / f"case_{case.key}" / case.media if case.media and not text_only else None
    if media is not None and not media.is_file():
        print(f"    (no clip at {media}; using the caption text only)")
        media = None
    print(f"\n[{case.key}] {case.title}  ({'upload ' + media.name if media else 'text'})")
    t0 = time.perf_counter()
    job_id = submit(client, case, media)
    follow_events(client, job_id, timeout_s)
    elapsed = time.perf_counter() - t0
    view = client.get(f"/verify/{job_id}").json()
    verify = client.get(f"/audit/{job_id}/verify").json()
    result: dict[str, Any] = {
        "case": case.key,
        "job_id": job_id,
        "input": "upload" if media else "text",
        "expected": case.expected,
        "verdict": view.get("verdict"),
        "confidence": view.get("confidence"),
        "abstain_reason": view.get("abstain_reason"),
        "matches_expected": view.get("verdict") == case.expected,
        "seconds": round(elapsed, 2),
        "audit_id": view.get("audit_id"),
        "audit_ok": verify.get("ok"),
        "degraded_tools": view.get("degraded_tools"),
        "tool_errors": len(view.get("tool_errors") or []),
        "labels": view.get("labels"),
        "report_eligible": view.get("report_eligible"),
        "legal_eligible": view.get("legal_eligible"),
    }
    if view.get("report_eligible"):
        package = client.post(f"/report/{job_id}").json()  # prepare only: never confirmed by a script
        result["report_prepared_audit_id"] = package.get("audit_id")
        result["report_platforms"] = [p["platform"] for p in package.get("platform_plans", [])]
    mark = "OK " if result["matches_expected"] else "DIFF"
    print(
        f"  {mark} {result['verdict']} (expected {case.expected}) conf={result['confidence']} "
        f"reason={result['abstain_reason']} in {result['seconds']} s; audit #{result['audit_id']} "
        f"verify ok={result['audit_ok']}"
    )
    if view.get("legal_eligible"):
        print("  legal draft available in the web app (needs the four acknowledgements; never sent)")
    return result


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default=os.environ.get("VERILOOP_API", f"http://127.0.0.1:{settings.port}"))
    parser.add_argument("--cases", default="abcd", help="subset of a,b,c,d (e.g. 'bc')")
    parser.add_argument("--text-only", action="store_true", help="never upload the demo clips")
    parser.add_argument("--api-key", default=os.environ.get("VERILOOP_API_KEY"))
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds per case")
    args = parser.parse_args(argv)

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    with httpx.Client(base_url=args.api, headers=headers, timeout=60) as client:
        health = client.get("/health").json()
        print(
            f"server {args.api}: status={health['status']} mode={health['mode']} "
            f"llm={health['llm_provider']} models={health['resolved_models']}"
        )
        if health["mode"] != "record":
            print("note: the server is not in TOOL_MODE=record, so no replay cassettes are written this run")
        results = [
            run_case(client, CASES[key], home=settings.home, text_only=args.text_only, timeout_s=args.timeout)
            for key in args.cases.replace(",", "")
            if key in CASES
        ]
    out_dir = settings.home / "demo" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"record-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(
        json.dumps({"api": args.api, "health": health, "results": results}, indent=2), encoding="utf-8"
    )
    print(f"\nsummary: {out}")
    return 0 if all(r["audit_ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
