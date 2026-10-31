"""reporter-agent core logic (no browser): fetch the confirmed report package, load per-platform
selectors, decide what the browser should do, and render the manual fallback.

Hard rule (D4 / G5): this agent NEVER submits a report. Selector files describe how to open a post's
report dialog and where to paste text; `final_submit` selectors are only ever highlighted for the
person, and `validate_selectors` rejects any file whose steps would click a submit/send control.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

AGENT_DIR = Path(__file__).resolve().parent
SELECTORS_DIR = AGENT_DIR / "selectors"
REPO_ROOT = AGENT_DIR.parent
ACTIONS = frozenset({"click", "fill", "wait"})
# A step may never click something that looks like the final submit (checked on selectors + labels).
SUBMIT_WORDS = re.compile(r"\b(submit|send|confirm report|done reporting)\b", re.IGNORECASE)


class AgentError(RuntimeError):
    """A problem the person must fix (bad attempt id, unconfirmed attempt, API unreachable...)."""


@dataclass(frozen=True)
class Step:
    action: str  # click | fill | wait
    selectors: list[str]  # alternatives, tried in order
    description: str
    optional: bool = False
    text: str = ""  # for fill: "prefilled_text" -> the confirmed text


@dataclass(frozen=True)
class PlatformSelectors:
    platform: str
    version: str
    steps: list[Step]
    final_submit: list[str] = field(default_factory=list)  # highlighted only, never clicked
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportPackage:
    """GET /report/{job}/attempts/{attempt} (only exists for attempts the person confirmed)."""

    attempt_id: str
    job_id: str
    platform: str
    method: str
    target_url: str | None
    prefilled_text: str
    report_category: str | None
    steps: list[str]
    card_url: str | None
    audit_page_url: str | None
    raw: dict[str, Any]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ReportPackage:
        if data.get("status") != "confirmed":
            raise AgentError(
                f"attempt {data.get('attempt_id')} is not confirmed; confirm it in the web app first"
            )
        plan = data.get("plan") or {}
        return cls(
            attempt_id=str(data["attempt_id"]),
            job_id=str(data["job_id"]),
            platform=str(data.get("platform") or plan.get("platform") or ""),
            method=str(data.get("method") or plan.get("method") or ""),
            target_url=plan.get("target_url"),
            prefilled_text=str(plan.get("prefilled_text") or data.get("preview_text") or ""),
            report_category=plan.get("report_category"),
            steps=[str(s) for s in plan.get("steps") or []],
            card_url=data.get("card_url") or plan.get("card_url"),
            audit_page_url=data.get("audit_page_url"),
            raw=data,
        )


def default_home() -> Path:
    """VERILOOP_HOME from the environment or the repo .env (runtime data stays off C:)."""
    env = os.environ.get("VERILOOP_HOME")
    if env:
        return Path(env)
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "VERILOOP_HOME" and value.strip():
                return Path(value.strip().strip('"').strip("'"))
    return Path("D:/veriloop")


def default_profile_dir() -> Path:
    env = os.environ.get("VERILOOP_REPORTER_PROFILE")
    return Path(env) if env else default_home() / "reporter-profile"


def fetch_package(
    api: str,
    job_id: str,
    attempt_id: str,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> ReportPackage:
    url = f"{api.rstrip('/')}/report/{job_id}/attempts/{attempt_id}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    own = client is None
    http = client or httpx.Client(timeout=15)
    try:
        resp = http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise AgentError(f"cannot reach the VeriLoop API at {api}: {exc}") from exc
    finally:
        if own:
            http.close()
    if resp.status_code == 404:
        raise AgentError("no confirmed report attempt with that id (confirm it in the web app first)")
    if resp.status_code == 401:
        raise AgentError("the API needs a key: pass --api-key or set VERILOOP_API_KEY")
    if resp.status_code >= 400:
        detail = resp.json().get("detail") if "json" in resp.headers.get("content-type", "") else resp.text
        raise AgentError(f"API error {resp.status_code}: {detail}")
    return ReportPackage.from_api(resp.json())


def validate_selectors(data: dict[str, Any], *, source: str = "selectors") -> PlatformSelectors:
    """Parse + check a selectors file. Raises ValueError for anything that could submit."""
    steps: list[Step] = []
    for i, raw in enumerate(data.get("steps") or []):
        action = raw.get("action")
        if action not in ACTIONS:
            raise ValueError(f"{source}: step {i} has unknown action {action!r}")
        selectors = [str(s) for s in raw.get("selectors") or []]
        if not selectors:
            raise ValueError(f"{source}: step {i} has no selectors")
        description = str(raw.get("description") or "")
        if action == "click" and any(SUBMIT_WORDS.search(s) for s in [*selectors, description]):
            raise ValueError(f"{source}: step {i} would click a submit/send control; the agent never submits")
        steps.append(
            Step(
                action=action,
                selectors=selectors,
                description=description,
                optional=bool(raw.get("optional", False)),
                text=str(raw.get("text") or ""),
            )
        )
    overlap = set(data.get("final_submit") or []) & {s for st in steps for s in st.selectors}
    if overlap:
        raise ValueError(f"{source}: final_submit selectors reused in steps: {sorted(overlap)}")
    return PlatformSelectors(
        platform=str(data["platform"]),
        version=str(data.get("version") or ""),
        steps=steps,
        final_submit=[str(s) for s in data.get("final_submit") or []],
        notes=[str(n) for n in data.get("notes") or []],
    )


def load_selectors(platform: str, directory: Path = SELECTORS_DIR) -> PlatformSelectors | None:
    path = directory / f"{platform}.json"
    if not path.is_file():
        return None
    return validate_selectors(json.loads(path.read_text(encoding="utf-8")), source=path.name)


def fill_text(step: Step, package: ReportPackage) -> str:
    if step.text in ("", "prefilled_text"):
        return package.prefilled_text
    return step.text


def manual_instructions(package: ReportPackage, reason: str | None = None) -> str:
    """What the person does by hand when automation is not possible (deep link + steps + text)."""
    lines = ["", "=" * 72, "VeriLoop reporter-agent: MANUAL STEPS"]
    if reason:
        lines.append(f"(automation stopped: {reason})")
    lines.append("=" * 72)
    if package.target_url:
        lines.append(f"Open: {package.target_url}")
    if package.report_category:
        lines.append(f"Report category: {package.report_category}")
    for i, step in enumerate(package.steps, 1):
        lines.append(f"{i}. {step}")
    lines += [
        "",
        "Text to paste (already confirmed by you in VeriLoop):",
        "-" * 72,
        package.prefilled_text,
        "-" * 72,
    ]
    if package.card_url:
        lines.append(f"Report card image: {package.card_url}")
    if package.audit_page_url:
        lines.append(f"Signed audit record: {package.audit_page_url}")
    lines.append("VeriLoop never submits anything: you press Submit yourself.")
    return "\n".join(lines)


def stop_message(package: ReportPackage) -> str:
    return (
        "\nSTOPPED before the final submit (VeriLoop never submits a report).\n"
        "Review the dialog in the browser window, choose the category"
        + (f" closest to '{package.report_category}'" if package.report_category else "")
        + ", check the pasted text, then press Submit yourself.\n"
        "Close the browser window (or press Enter here) when you are done."
    )
