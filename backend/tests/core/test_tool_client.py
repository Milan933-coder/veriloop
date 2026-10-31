import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from veriloop.contracts import ToolState
from veriloop.core.cache import ToolCache
from veriloop.core.errors import ToolAttemptError, ToolCallFailed
from veriloop.core.logging import register_secrets
from veriloop.core.testing import FakeClock, RecordingSleep
from veriloop.core.tool_client import ToolClient, call_key_hash


def counter_fn(results: list[Any]) -> tuple[Callable, list[int]]:
    """fn that returns/raises results[i] on call i (last one repeats)."""
    calls = [0]

    async def fn() -> Any:
        i = min(calls[0], len(results) - 1)
        calls[0] += 1
        item = results[i]
        if isinstance(item, BaseException):
            raise item
        return item

    return fn, calls


def make_client(settings, clock: FakeClock, sleep: RecordingSleep | None = None) -> ToolClient:
    return ToolClient(
        settings,
        cache=ToolCache(clock=clock),
        clock=clock,
        sleep=sleep or RecordingSleep(clock),
        rng=lambda: 0.0,
    )


async def test_success_returns_json_normalized_value(tool_client: ToolClient) -> None:
    fn, calls = counter_fn([{"items": ("a", "b"), "n": 1}])
    res = await tool_client.call("gdelt", "doc", {"q": "x"}, fn)
    assert res.ok and res.value == {"items": ["a", "b"], "n": 1} and res.attempts == 1
    assert not res.from_cache and not res.replayed and calls[0] == 1


async def test_unknown_tool_is_a_programming_error(tool_client: ToolClient) -> None:
    fn, _ = counter_fn([1])
    with pytest.raises(ValueError):
        await tool_client.call("nope", "op", {}, fn)


async def test_unconfigured_tool_short_circuits_without_calling(tool_client: ToolClient) -> None:
    fn, calls = counter_fn([{"ok": 1}])
    res = await tool_client.call("serper", "search", {"q": "x"}, fn)
    assert not res.ok and res.error.error_type == "unconfigured" and "SERPER_API_KEY" in res.error.message
    assert calls[0] == 0
    with pytest.raises(ToolCallFailed) as exc_info:
        await tool_client.call_or_raise("sightengine", "check", {}, fn)
    assert exc_info.value.error.error_type == "unconfigured"


async def test_configured_keyed_tool_runs(make_settings, fake_clock) -> None:
    client = make_client(make_settings(serper_api_key="serper-key-123"), fake_clock)
    fn, calls = counter_fn([{"organic": []}])
    assert (await client.call_or_raise("serper", "search", {"q": "x"}, fn)) == {"organic": []}
    assert calls[0] == 1
    await client.aclose()


async def test_timeout_is_retried_then_reported(settings, fake_clock, fake_sleep) -> None:
    client = make_client(settings, fake_clock, fake_sleep)

    async def slow() -> dict:
        await asyncio.sleep(5)
        return {}

    res = await client.call("gdelt", "doc", {"q": "slow"}, slow, timeout_s=0.02)
    assert not res.ok and res.error.error_type == "timeout" and res.attempts == 3
    assert res.error.attempts == 3
    assert fake_sleep.delays == [0.5, 1.0]  # exp backoff x2 between the 3 attempts
    await client.aclose()


async def test_transient_errors_retried_until_success(tool_client: ToolClient, fake_sleep) -> None:
    fn, calls = counter_fn(
        [httpx.ConnectError("dns"), ToolAttemptError("http_error", "503", retryable=True), {"v": 1}]
    )
    res = await tool_client.call("wikidata", "sparql", {"q": "1"}, fn)
    assert res.ok and res.value == {"v": 1} and res.attempts == 3 and calls[0] == 3
    assert len(fake_sleep.delays) == 2


async def test_non_retryable_errors_fail_fast(tool_client: ToolClient, fake_sleep) -> None:
    fn, calls = counter_fn([ToolAttemptError("http_error", "HTTP 404", retryable=False)])
    res = await tool_client.call("wikipedia", "search", {"q": "x"}, fn)
    assert not res.ok and res.error.error_type == "http_error" and calls[0] == 1 and fake_sleep.delays == []
    fn2, _ = counter_fn([ValueError("bad json")])
    res2 = await tool_client.call("wikipedia", "search", {"q": "y"}, fn2)
    assert res2.error.error_type == "parse_error"


async def test_http_status_errors_from_respx_are_classified(tool_client: ToolClient, fake_sleep) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, json={"articles": []}),
            ]
        )
        mock.get("https://www.wikidata.org/w/api.php").mock(return_value=httpx.Response(403))

        async def gdelt() -> Any:
            r = await tool_client.http.get(
                "https://api.gdeltproject.org/api/v2/doc/doc", params={"query": "x"}
            )
            r.raise_for_status()
            return r.json()

        res = await tool_client.call("gdelt", "doc", {"query": "x"}, gdelt)
        assert res.ok and res.attempts == 2 and fake_sleep.delays == [2.0]  # honoured Retry-After

        async def wikidata() -> Any:
            r = await tool_client.http.get("https://www.wikidata.org/w/api.php")
            r.raise_for_status()
            return r.json()

        res2 = await tool_client.call("wikidata", "search", {"q": "x"}, wikidata)
        assert not res2.ok and res2.error.error_type == "http_error" and res2.attempts == 1


async def test_rate_limit_longer_than_budget_is_not_waited_for(tool_client: ToolClient, fake_sleep) -> None:
    fn, calls = counter_fn([ToolAttemptError("rate_limited", "429", retryable=True, retry_after_s=120)])
    res = await tool_client.call("gdelt", "doc", {"q": "rl"}, fn)
    assert res.error.error_type == "rate_limited" and calls[0] == 1 and fake_sleep.delays == []
    assert tool_client.rate_limited_tools() == ["gdelt"]


async def test_breaker_opens_then_half_open_probe(tool_client: ToolClient, fake_clock: FakeClock) -> None:
    failing, calls = counter_fn([httpx.ConnectError("down")])
    for i in range(5):
        res = await tool_client.call("gdelt", "doc", {"i": i}, failing, retries=0)
        assert res.error.error_type == "http_error"
    assert tool_client.status()["gdelt"].state is ToolState.OPEN
    res = await tool_client.call("gdelt", "doc", {"i": 99}, failing)
    assert res.error.error_type == "circuit_open" and calls[0] == 5  # not called while open

    fake_clock.advance(31)
    assert tool_client.status()["gdelt"].state is ToolState.DEGRADED  # half-open
    res = await tool_client.call("gdelt", "doc", {"i": 100}, failing)  # probe: single attempt
    assert res.error.error_type == "http_error" and res.attempts == 1 and calls[0] == 6
    assert tool_client.status()["gdelt"].state is ToolState.OPEN  # probe failed -> re-opened

    fake_clock.advance(31)
    ok_fn, _ = counter_fn([{"fine": True}])
    res = await tool_client.call("gdelt", "doc", {"i": 101}, ok_fn)
    assert res.ok
    assert tool_client.status()["gdelt"].state is ToolState.OK


async def test_chaos_injection(tool_client: ToolClient) -> None:
    fn, calls = counter_fn([{"x": 1}])
    tool_client.chaos.set("gnews_rss", "timeout")
    res = await tool_client.call("gnews_rss", "search", {"q": "a"}, fn)
    assert res.error.error_type == "timeout" and res.error.message.startswith("chaos:")
    assert tool_client.status()["gnews_rss"].state is ToolState.CHAOS
    tool_client.chaos.set("gnews_rss", "error")
    res = await tool_client.call("gnews_rss", "search", {"q": "a"}, fn)
    assert res.error.error_type == "chaos" and calls[0] == 0
    tool_client.chaos.set("gnews_rss", "off")
    assert (await tool_client.call("gnews_rss", "search", {"q": "a"}, fn)).ok and calls[0] == 1


async def test_cache_hit_skips_fn_and_returns_copies(tool_client: ToolClient, fake_clock: FakeClock) -> None:
    fn, calls = counter_fn([{"items": [1]}])
    first = await tool_client.call("gnews_rss", "search", {"q": "c"}, fn)
    first.value["items"].append("mutated")
    second = await tool_client.call("gnews_rss", "search", {"q": "c"}, fn)
    assert second.from_cache and second.value == {"items": [1]} and calls[0] == 1
    fake_clock.advance(31 * 60)  # gnews TTL is 30 min
    third = await tool_client.call("gnews_rss", "search", {"q": "c"}, fn)
    assert not third.from_cache and calls[0] == 2
    await tool_client.call("gnews_rss", "search", {"q": "d"}, fn, cache_ttl_s=0)
    assert not (await tool_client.call("gnews_rss", "search", {"q": "d"}, fn, cache_ttl_s=0)).from_cache


async def test_record_then_replay_round_trip_with_secret_redaction(make_settings, fake_clock) -> None:
    register_secrets(["sk-very-secret-key-99"])
    recorder = make_client(make_settings(tool_mode="record"), fake_clock)
    payload = {"result": "ok", "echo": "Authorization: Bearer sk-very-secret-key-99", "token": "t-abc-123456"}
    fn, _ = counter_fn([payload])
    key = {"q": "round trip", "big": "x" * 5000}
    live = await recorder.call("wikipedia", "search", key, fn)
    assert live.ok and live.value == payload  # the live caller still gets the real value

    path = recorder.cassette_path("wikipedia", "search", key)
    assert path.name == f"{call_key_hash('search', key)}.json" and path.parent.name == "wikipedia"
    text = path.read_text(encoding="utf-8")
    assert "sk-very-secret-key-99" not in text and "t-abc-123456" not in text
    doc = json.loads(text)
    assert doc["op"] == "search" and doc["key"]["big"].startswith("<sha256:")
    await recorder.aclose()

    replayer = make_client(make_settings(tool_mode="replay"), fake_clock)
    never, calls = counter_fn([{"should": "not be called"}])
    res = await replayer.call("wikipedia", "search", key, never)
    assert res.ok and res.replayed and calls[0] == 0
    assert res.value["result"] == "ok" and "sk-very-secret-key-99" not in json.dumps(res.value)
    miss = await replayer.call("wikipedia", "search", {"q": "never recorded"}, never)
    assert not miss.ok and miss.error.error_type == "replay_miss" and calls[0] == 0
    # replay works for keyed tools without the key (a recorded real response is not fake data)
    assert (await replayer.call("serper", "search", {"q": "z"}, never)).error.error_type == "replay_miss"
    await replayer.aclose()


async def test_non_json_return_is_parse_error(tool_client: ToolClient) -> None:
    async def fn() -> Any:
        return object()

    res = await tool_client.call("page_fetch", "get", {"u": 1}, fn)
    assert res.error.error_type == "parse_error"


async def test_status_lists_keyless_ok_and_keyed_unconfigured(tool_client: ToolClient) -> None:
    status = tool_client.status()
    assert status["gnews_rss"].configured and status["gnews_rss"].state is ToolState.OK
    assert status["claimreview_feed"].state is ToolState.OK
    assert not status["serper"].configured and status["serper"].state is ToolState.UNCONFIGURED
    assert "SERPER_API_KEY" in (status["serper"].last_error or "")
    tool_client.disable("llm_primary", "LLM_PROVIDER=none")
    assert tool_client.status()["llm_primary"].state is ToolState.DISABLED
    fn, _ = counter_fn([httpx.ReadTimeout("t")])
    await tool_client.call("pib_rss", "feed", {}, fn, retries=0)
    st = tool_client.status()["pib_rss"]
    assert (
        st.state is ToolState.DEGRADED and st.calls == 1 and st.failures == 1 and "timeout" in st.last_error
    )


async def test_secondary_for_skips_unconfigured_and_open(tool_client: ToolClient) -> None:
    assert tool_client.secondary_for("serper") == ["gnews_rss", "bing_news_rss"]
    tool_client.chaos.set("gnews_rss", "error")
    assert tool_client.secondary_for("serper") == ["bing_news_rss"]
    assert tool_client.secondary_for("sightengine") == []


async def test_failures_are_logged_with_tool_and_error_type(tool_client: ToolClient) -> None:
    import structlog

    fn, _ = counter_fn([ToolAttemptError("http_error", "HTTP 400 bad", retryable=False)])
    with structlog.testing.capture_logs() as logs:
        await tool_client.call("oembed", "get", {"u": "x"}, fn)
    failed = [e for e in logs if e["event"] == "tool_call_failed"]
    assert failed and failed[0]["tool"] == "oembed" and failed[0]["error_type"] == "http_error"
