"""reporter-agent non-browser parts: package fetch, human-gate checks, selector safety, fallback text.

Run: C:/Users/lokar/.veriloop/venv/Scripts/python.exe -m pytest reporter-agent/tests -q -p no:cacheprovider
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import agent_core  # noqa: E402
import reporter_agent  # noqa: E402
from agent_core import (  # noqa: E402
    AgentError,
    ReportPackage,
    fetch_package,
    load_selectors,
    manual_instructions,
    validate_selectors,
)

PACKAGE = {
    "attempt_id": "ra_0123",
    "job_id": "0123456789abcdef",
    "status": "confirmed",
    "confirmed_at": "2026-10-31T10:00:00+00:00",
    "platform": "x",
    "method": "playwright_guided",
    "plan": {
        "platform": "x",
        "target_url": "https://x.com/someone/status/123",
        "method": "playwright_guided",
        "steps": ["Open the post", "Click ...", "Review everything, then press Submit yourself."],
        "prefilled_text": "Verified FALSE (86% confidence). Sources: https://newschecker.in/a",
        "report_category": "Misleading information",
        "card_url": "http://localhost:8000/report/0123456789abcdef/card.png",
    },
    "preview_text": "VeriLoop verification report ...",
    "card_url": "http://localhost:8000/report/0123456789abcdef/card.png",
    "audit_page_url": "http://localhost:8000/a/0123456789abcdef",
}


def _client(status: int, body: object, seen: list[httpx.Request] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_confirmed_package_with_api_key() -> None:
    seen: list[httpx.Request] = []
    pkg = fetch_package(
        "http://api/", "0123456789abcdef", "ra_0123", api_key="k", client=_client(200, PACKAGE, seen)
    )
    assert seen[0].url.path == "/report/0123456789abcdef/attempts/ra_0123"
    assert seen[0].headers["x-api-key"] == "k"
    assert pkg.platform == "x" and pkg.method == "playwright_guided"
    assert pkg.target_url == "https://x.com/someone/status/123" and pkg.prefilled_text.startswith(
        "Verified FALSE"
    )


@pytest.mark.parametrize(
    ("status", "message"),
    [(404, "no confirmed report attempt"), (401, "needs a key"), (500, "API error 500")],
)
def test_fetch_errors_are_explained(status: int, message: str) -> None:
    with pytest.raises(AgentError, match=message):
        fetch_package("http://api", "j", "a", client=_client(status, {"detail": "boom"}))


def test_unconfirmed_attempt_is_refused() -> None:
    with pytest.raises(AgentError, match="not confirmed"):
        ReportPackage.from_api({**PACKAGE, "status": "prepared"})


@pytest.mark.parametrize("platform", ["x", "instagram", "facebook", "youtube"])
def test_shipped_selectors_are_valid_and_never_click_submit(platform: str) -> None:
    sel = load_selectors(platform)
    assert sel is not None and sel.platform == platform and sel.steps
    assert sel.final_submit, "the submit control must be listed (to highlight it, never to click it)"
    clicks = [s for s in sel.steps if s.action == "click"]
    assert clicks and all(not set(s.selectors) & set(sel.final_submit) for s in clicks)
    assert any(s.action == "fill" for s in sel.steps)


@pytest.mark.parametrize(
    "step",
    [
        {"action": "click", "selectors": ['button:has-text("Submit")'], "description": "next"},
        {"action": "click", "selectors": ["#go"], "description": "Send the report"},
        {"action": "hover", "selectors": ["#x"], "description": "?"},
        {"action": "click", "selectors": [], "description": "nothing"},
    ],
)
def test_unsafe_or_broken_selector_files_are_rejected(step: dict) -> None:
    with pytest.raises(ValueError):
        validate_selectors({"platform": "x", "steps": [step]})


def test_final_submit_reused_in_steps_is_rejected() -> None:
    data = {
        "platform": "x",
        "final_submit": ["#ok"],
        "steps": [{"action": "click", "selectors": ["#ok"], "description": "open menu"}],
    }
    with pytest.raises(ValueError, match="final_submit"):
        validate_selectors(data)


def test_manual_instructions_have_link_steps_text_and_no_submit() -> None:
    text = manual_instructions(ReportPackage.from_api(PACKAGE), "could not find the menu")
    assert "https://x.com/someone/status/123" in text and "could not find the menu" in text
    assert PACKAGE["plan"]["prefilled_text"] in text  # type: ignore[index]
    assert "never submits" in text and "card.png" in text


def test_default_profile_dir_is_under_veriloop_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERILOOP_REPORTER_PROFILE", raising=False)
    monkeypatch.setenv("VERILOOP_HOME", "D:/veriloop")
    assert agent_core.default_profile_dir() == Path("D:/veriloop") / "reporter-profile"
    monkeypatch.setenv("VERILOOP_REPORTER_PROFILE", "E:/profiles/vl")
    assert agent_core.default_profile_dir() == Path("E:/profiles/vl")


def test_cli_dry_run_and_card_plans_never_open_a_browser(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, str]] = []

    def fake_fetch(api: str, job: str, attempt: str, *, api_key: str | None = None) -> ReportPackage:
        calls.append((api, job, attempt))
        return ReportPackage.from_api(PACKAGE)

    monkeypatch.setattr(reporter_agent, "fetch_package", fake_fetch)
    assert (
        reporter_agent.main(["--api", "http://h:8000", "--job", "j1", "--attempt", "ra_1", "--dry-run"]) == 0
    )
    out = capsys.readouterr().out
    assert calls == [("http://h:8000", "j1", "ra_1")] and "step 1: click" in out and "MANUAL STEPS" in out

    card = {
        **PACKAGE,
        "platform": "whatsapp",
        "method": "report_card",
        "plan": {**PACKAGE["plan"], "platform": "whatsapp", "method": "report_card"},
    }  # type: ignore[dict-item]
    monkeypatch.setattr(reporter_agent, "fetch_package", lambda *a, **k: ReportPackage.from_api(card))
    assert reporter_agent.main(["--job", "j1", "--attempt", "ra_2"]) == 0
    assert "MANUAL STEPS" in capsys.readouterr().out


def test_cli_reports_agent_errors(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    def boom(*a: object, **k: object) -> ReportPackage:
        raise AgentError("no confirmed report attempt with that id")

    monkeypatch.setattr(reporter_agent, "fetch_package", boom)
    assert reporter_agent.main(["--job", "j", "--attempt", "a"]) == 1
    assert "no confirmed report attempt" in capsys.readouterr().err
