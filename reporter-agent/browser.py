"""Playwright driver: headed Chromium with a persistent profile (the person stays logged in to the
platforms between runs). Opens the post, walks the report-dialog steps from selectors/<platform>.json,
pastes the confirmed text where a text box exists, highlights the final Submit button and STOPS.
It never clicks a final submit control (D4). Any selector failure switches to manual instructions.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_core import PlatformSelectors, ReportPackage, Step, fill_text, manual_instructions, stop_message

STEP_TIMEOUT_MS = 8_000
HIGHLIGHT_JS = """(els) => els.forEach((el) => {
  el.style.outline = '4px solid #f59e0b'; el.style.outlineOffset = '3px';
  el.setAttribute('title', 'VeriLoop: review everything, then press this yourself');
})"""


def _run_step(page, step: Step, package: ReportPackage) -> bool:  # type: ignore[no-untyped-def]
    """Try each alternative selector; True when one worked."""
    for selector in step.selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
            if step.action == "click":
                locator.click(timeout=STEP_TIMEOUT_MS)
            elif step.action == "fill":
                locator.fill(fill_text(step, package), timeout=STEP_TIMEOUT_MS)
            return True
        except Exception:  # timeout / detached / not editable: try the next alternative
            continue
    return False


def _wait_for_person(context) -> None:  # type: ignore[no-untyped-def]
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    try:
        context.close()
    except Exception:
        pass


def run_browser(package: ReportPackage, selectors: PlatformSelectors | None, profile_dir: Path) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(manual_instructions(package, "Playwright is not installed in this environment"))
        return 2
    if not package.target_url:
        print(manual_instructions(package, "the confirmed plan has no post URL to open"))
        return 0
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir), headless=False, viewport={"width": 1280, "height": 900}
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(package.target_url, wait_until="domcontentloaded", timeout=45_000)
        print(f"Opened {package.target_url} (log in once in this window if the platform asks).")
        failed: str | None = None
        if selectors is None:
            failed = f"no selectors for platform {package.platform!r}"
        else:
            for step in selectors.steps:
                ok = _run_step(page, step, package)
                print(f"  [{'ok' if ok else ('skip' if step.optional else 'FAIL')}] {step.description}")
                if not ok and not step.optional:
                    failed = f"could not find: {step.description}"
                    break
            if failed is None and selectors.final_submit:
                for selector in selectors.final_submit:
                    try:
                        page.locator(selector).evaluate_all(HIGHLIGHT_JS)
                    except Exception:
                        continue
        if failed is not None:
            print(manual_instructions(package, failed))
        print(stop_message(package))
        sys.stdout.flush()
        _wait_for_person(context)
    return 0
