"""VeriLoop reporter-agent (D-10): pre-fill a platform's report dialog for a CONFIRMED report, then stop.

    python reporter-agent/reporter_agent.py --api http://localhost:8000 --job <job_id> --attempt <attempt_id>

The exact command is returned by POST /report/{job_id}/confirm (`reporter_agent_command`). The agent
fetches GET /report/{job_id}/attempts/{attempt_id} (only confirmed attempts exist there), opens the
post in a HEADED browser with a persistent profile (default VERILOOP_HOME/reporter-profile, e.g.
D:/veriloop/reporter-profile), opens the report dialog, pastes the confirmed text where possible and
STOPS before the final submit. It never submits (D4). `--dry-run` prints the plan without a browser.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_core import (  # sibling module of this standalone script (path set above)
    AgentError,
    default_home,
    default_profile_dir,
    fetch_package,
    load_selectors,
    manual_instructions,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reporter-agent", description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default=os.environ.get("VERILOOP_API", "http://localhost:8000"))
    parser.add_argument("--job", required=True, help="job id")
    parser.add_argument("--attempt", required=True, help="confirmed report attempt id (ra_...)")
    parser.add_argument(
        "--api-key", default=os.environ.get("VERILOOP_API_KEY"), help="X-API-Key, if required"
    )
    parser.add_argument("--profile", type=Path, default=None, help="persistent browser profile directory")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and selectors; no browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        package = fetch_package(args.api, args.job, args.attempt, api_key=args.api_key)
    except AgentError as exc:
        print(f"reporter-agent: {exc}", file=sys.stderr)
        return 1
    print(
        f"Confirmed report {package.attempt_id} for job {package.job_id}: "
        f"{package.platform} via {package.method}"
    )
    if package.method != "playwright_guided":
        # report_card (Telegram/WhatsApp) and deeplink_guided plans are done by hand by design.
        print(manual_instructions(package))
        return 0
    try:
        selectors = load_selectors(package.platform)
    except ValueError as exc:  # an unsafe or broken selectors file: never automate with it
        print(f"reporter-agent: refusing selectors: {exc}", file=sys.stderr)
        selectors = None
    if args.dry_run:
        for i, step in enumerate(selectors.steps if selectors else [], 1):
            print(f"  step {i}: {step.action} {step.description} -> {step.selectors[0]}")
        print(manual_instructions(package, "dry run"))
        return 0
    # Playwright/Chromium write artifacts to %TEMP%, which is on the nearly full C: drive.
    tmp = default_home() / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    for var in ("TMP", "TEMP", "TMPDIR"):
        os.environ[var] = str(tmp)
    from browser import run_browser

    return run_browser(package, selectors, args.profile or default_profile_dir())


if __name__ == "__main__":
    raise SystemExit(main())
