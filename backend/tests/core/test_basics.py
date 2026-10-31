"""ids, hashing, executor, chaos, tools registry, errors."""

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from veriloop.contracts import ToolError
from veriloop.core.chaos import ChaosState
from veriloop.core.config import REPO_ROOT
from veriloop.core.errors import ProblemError, ToolCallFailed
from veriloop.core.executor import run_cpu
from veriloop.core.hashing import canonical_json, sha256_hex, stable_id
from veriloop.core.ids import is_job_id, job_id_timestamp_ms, new_id, new_job_id
from veriloop.core.tools_registry import TOOL_NAMES, apply_overrides, default_registry, load_registry

SPEC_TOOLS = (
    "factcheck claimreview_feed gnews_rss bing_news_rss wire_rss pib_rss gdelt serper wikipedia wikidata "
    "vision_web sightengine hf_inference llm_primary llm_secondary oembed page_fetch ytdlp playwright_fetch"
).split()
SPEC_KEYLESS = (
    "claimreview_feed gnews_rss bing_news_rss wire_rss pib_rss gdelt wikipedia wikidata oembed page_fetch "
    "ytdlp playwright_fetch"
).split()


def test_job_ids_short_urlsafe_sortable_unique() -> None:
    ids = [new_job_id(now_ms=1_700_000_000_000 + i) for i in range(50)]
    assert ids == sorted(ids)
    assert all(len(i) == 16 and is_job_id(i) and i.isalnum() and i == i.lower() for i in ids)
    assert len({new_job_id() for _ in range(2000)}) == 2000
    assert job_id_timestamp_ms(new_job_id(now_ms=1_761_900_000_123)) == 1_761_900_000_123
    assert new_id("ra").startswith("ra_") and is_job_id(new_id("ra")[3:])
    assert not is_job_id("../../etc/passwd")


def test_canonical_json_rules() -> None:
    dt = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    text = canonical_json({"b": 1.23456789, "a": "हिंदी", "t": dt, "n": None})
    assert text == '{"a":"हिंदी","b":1.234568,"n":null,"t":"2026-10-31T12:00:00+00:00"}'
    assert canonical_json({"x": 1, "y": 2}) == canonical_json({"y": 2, "x": 1})
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    assert len(sha256_hex("abc")) == 64
    assert stable_id("u", "c") == stable_id("u", "c") != stable_id("u", "d")


async def test_run_cpu_runs_in_worker_thread_and_propagates_errors() -> None:
    main = threading.get_ident()
    assert await run_cpu(lambda a, b: (a + b, threading.get_ident() != main), 2, b=3) == (5, True)

    def boom() -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await run_cpu(boom)
    results = await asyncio.gather(*(run_cpu(pow, i, 2) for i in range(10)))
    assert results == [i * i for i in range(10)]


def test_chaos_state_validation() -> None:
    chaos = ChaosState()
    chaos.set("serper", "timeout")
    assert chaos.get("serper") == "timeout" and chaos.active() == {"serper": "timeout"}
    chaos.set("serper", "off")
    assert chaos.get("serper") == "off" and chaos.active() == {}
    with pytest.raises(ValueError):
        chaos.set("nope", "error")
    with pytest.raises(ValueError):
        chaos.set("serper", "explode")


def test_registry_has_exactly_the_spec_tools() -> None:
    assert list(TOOL_NAMES) == SPEC_TOOLS
    reg = default_registry()
    assert sorted(n for n, s in reg.items() if s.keyless) == sorted(SPEC_KEYLESS)
    assert reg["serper"].secondary == ("gnews_rss", "bing_news_rss")
    assert reg["factcheck"].secondary == ("claimreview_feed",)
    assert reg["gnews_rss"].secondary == ("bing_news_rss",) and reg["bing_news_rss"].secondary == (
        "gnews_rss",
    )
    for spec in reg.values():
        assert all(s in reg for s in spec.secondary)


def test_tools_yaml_is_valid_and_llm_timeout_follows_settings() -> None:
    path = REPO_ROOT / "config" / "tools.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(data["tools"]) <= set(TOOL_NAMES)
    reg = load_registry(path, llm_timeout_s=12.5)
    assert reg["llm_primary"].timeout_s == 12.5 and reg["llm_secondary"].timeout_s == 12.5


def test_registry_overrides_fail_loudly(tmp_path: Path) -> None:
    reg = apply_overrides(default_registry(), {"gdelt": {"timeout_s": 3, "cache_ttl_s": 5}})
    assert reg["gdelt"].timeout_s == 3.0 and reg["gdelt"].cache_ttl_s == 5
    with pytest.raises(ValueError):
        apply_overrides(default_registry(), {"bogus": {"timeout_s": 1}})
    with pytest.raises(ValueError):
        apply_overrides(default_registry(), {"gdelt": {"colour": "red"}})
    with pytest.raises(ValueError):
        apply_overrides(default_registry(), {"gdelt": {"secondary": ["bogus"]}})


def test_errors() -> None:
    err = ToolError(tool="serper", op="search", error_type="timeout", message="slow")
    exc = ToolCallFailed(err)
    assert exc.error is err and "serper:search" in str(exc)
    problem = ProblemError(404, "Not Found", "no such job", instance="/verify/x").to_problem(request_id="r1")
    assert problem == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "no such job",
        "instance": "/verify/x",
        "request_id": "r1",
    }
