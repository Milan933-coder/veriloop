"""AicreditsLLM against respx mocks (no network), DisabledLLM, FakeLLM, boot-time model resolution."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
import structlog
from pydantic import BaseModel

from veriloop.bootstrap import build_llm_clients
from veriloop.contracts import ToolState
from veriloop.core.aicredits import AicreditsAPI
from veriloop.core.cache import ToolCache
from veriloop.core.errors import LLMDisabled, ModelNotAllowed, ModelResolutionError, ToolCallFailed
from veriloop.core.llm import AicreditsLLM, DisabledLLM
from veriloop.core.logging import register_secrets
from veriloop.core.model_registry import ModelRegistry
from veriloop.core.testing import FakeClock, FakeLLM, RecordingSleep
from veriloop.core.tool_client import ToolClient

BASE = "https://api.aicredits.in/v1"
KEY = "sk-unit-test-key-0123456789"
MODEL = "openai/gpt-6-luna"


class Stance(BaseModel):
    stance: str
    confidence: float
    quote: str | None = None


def completion(content: Any = None, *, refusal: str | None = None, finish: str = "stop") -> dict:
    if content is not None and not isinstance(content, str):
        content = json.dumps(content)
    return {
        "id": "gen-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "native_finish_reason": "completed",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "refusal": refusal,
                    "reasoning": "hidden",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 9,
            "completion_tokens_details": {"reasoning_tokens": 0},
            "cost": 0.0012,
            "latency_ms": 900,
        },
    }


HEADERS = {"X-Request-Id": "req-123", "X-Ratelimit-Remaining": "999", "X-Ratelimit-Limit": "1000"}


@pytest.fixture
def llm_settings(make_settings):
    register_secrets([KEY])
    return make_settings(llm_provider="aicredits", llm_api_key=KEY)


@pytest.fixture
async def llm_tools(llm_settings) -> AsyncIterator[tuple[ToolClient, RecordingSleep]]:
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    tools = ToolClient(llm_settings, cache=ToolCache(clock=clock), clock=clock, sleep=sleep, rng=lambda: 0.0)
    yield tools, sleep
    await tools.aclose()


@pytest.fixture
def llm(llm_settings, llm_tools) -> AicreditsLLM:
    tools, _ = llm_tools
    return AicreditsLLM(
        llm_settings, tools, AicreditsAPI(llm_settings, tools.http), ModelRegistry([MODEL]), model_id=MODEL
    )


def sent_json(route: respx.Route, i: int = -1) -> dict:
    return json.loads(route.calls[i].request.content)


async def test_success_sends_strict_schema_and_parses(llm: AicreditsLLM) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=httpx.Response(
                200, json=completion({"stance": "support", "confidence": 0.9, "quote": None}), headers=HEADERS
            )
        )
        with structlog.testing.capture_logs() as logs:
            out = await llm.complete_json(task="stance", system="sys", user="claim + evidence", schema=Stance)
    assert out == Stance(stance="support", confidence=0.9)
    req = route.calls[0].request
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    body = sent_json(route)
    assert (
        body["model"] == MODEL
        and body["reasoning_effort"] == "none"
        and body["max_completion_tokens"] == 2048
    )
    assert "temperature" not in body and "plugins" not in body and "web_search_options" not in body
    rf = body["response_format"]
    assert (
        rf["type"] == "json_schema"
        and rf["json_schema"]["strict"] is True
        and rf["json_schema"]["name"] == "stance"
    )
    assert rf["json_schema"]["schema"]["required"] == ["stance", "confidence", "quote"]
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "claim + evidence"},
    ]
    call_log = next(e for e in logs if e["event"] == "llm_call")
    assert (
        call_log["cost_inr"] == 0.0012
        and call_log["request_id"] == "req-123"
        and call_log["prompt_tokens"] == 20
    )
    assert KEY not in json.dumps(logs, default=str)


async def test_401_is_fatal_and_not_retried(llm: AicreditsLLM, llm_tools) -> None:
    _, sleep = llm_tools
    err = {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}}
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/chat/completions").mock(return_value=httpx.Response(401, json=err))
        with structlog.testing.capture_logs() as logs, pytest.raises(ToolCallFailed) as exc_info:
            await llm.complete_json(task="t", system="s", user="u", schema=Stance)
    assert route.call_count == 1 and sleep.delays == []
    e = exc_info.value.error
    assert e.error_type == "http_error" and "401" in e.message and "Invalid API Key" in e.message
    assert KEY not in e.message and KEY not in json.dumps(logs, default=str)


async def test_wrapped_upstream_400_is_unwrapped(llm: AicreditsLLM) -> None:
    raw = json.dumps(
        {
            "error": {
                "message": "Unsupported value: 'reasoning_effort' does not support 'turbo'.",
                "type": "invalid_request_error",
                "param": "reasoning_effort",
                "code": "unsupported_value",
            }
        }
    )
    body = {"error": {"message": "Provider returned error", "code": 400, "metadata": {"raw": raw}}}
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=httpx.Response(400, json=body, headers=HEADERS)
        )
        with pytest.raises(ToolCallFailed) as exc_info:
            await llm.complete_json(task="t", system="s", user="u", schema=Stance)
    msg = exc_info.value.error.message
    assert route.call_count == 1
    assert "Provider returned error" in msg and "unsupported_value" in msg and "reasoning_effort" in msg
    assert "req-123" in msg


async def test_response_format_rejection_falls_back_to_json_object(llm: AicreditsLLM) -> None:
    raw = json.dumps(
        {
            "error": {
                "message": "Invalid schema for response_format 'stance'",
                "param": "response_format",
                "code": "invalid_json_schema",
            }
        }
    )
    rejected = {"error": {"message": "Provider returned error", "code": 400, "metadata": {"raw": raw}}}
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/chat/completions").mock(
            side_effect=[
                httpx.Response(400, json=rejected),
                httpx.Response(200, json=completion({"stance": "refute", "confidence": 0.7, "quote": "q"})),
            ]
        )
        out = await llm.complete_json(task="stance", system="sys", user="u", schema=Stance)
    assert out.stance == "refute" and route.call_count == 2
    second = sent_json(route, 1)
    assert second["response_format"] == {"type": "json_object"}
    assert "JSON schema" in second["messages"][0]["content"]


async def test_refusal_raises(llm: AicreditsLLM) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=completion(refusal="I can't help"))
        )
        with pytest.raises(ToolCallFailed) as exc_info:
            await llm.complete_json(task="t", system="s", user="u", schema=Stance)
    assert exc_info.value.error.error_type == "refusal" and "I can't help" in exc_info.value.error.message


async def test_invalid_json_gets_one_repair_round_trip(llm: AicreditsLLM) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/chat/completions").mock(
            side_effect=[
                httpx.Response(200, json=completion("{not json")),
                httpx.Response(
                    200,
                    json=completion('```json\n{"stance": "neutral", "confidence": 0.5, "quote": null}\n```'),
                ),
            ]
        )
        out = await llm.complete_json(task="stance", system="s", user="u", schema=Stance)
    assert out.stance == "neutral" and route.call_count == 2
    repair_msgs = sent_json(route, 1)["messages"]
    assert repair_msgs[-2] == {"role": "assistant", "content": "{not json"}
    assert "did not validate" in repair_msgs[-1]["content"]


async def test_repair_failure_raises_parse_error(llm: AicreditsLLM) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=httpx.Response(200, json=completion({"wrong": 1}))
        )
        with pytest.raises(ToolCallFailed) as exc_info:
            await llm.complete_json(task="stance", system="s", user="u", schema=Stance)
    assert route.call_count == 2 and exc_info.value.error.error_type == "parse_error"


async def test_429_is_retried_honouring_ratelimit_reset(llm: AicreditsLLM, llm_tools) -> None:
    _, sleep = llm_tools
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/chat/completions").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={"error": {"code": 429, "message": "Rate limit exceeded"}},
                    headers={"X-Ratelimit-Reset": "3", "X-Ratelimit-Remaining": "0"},
                ),
                httpx.Response(200, json=completion({"stance": "support", "confidence": 1, "quote": None})),
            ]
        )
        out = await llm.complete_json(task="t", system="s", user="u", schema=Stance)
    assert out.confidence == 1.0 and route.call_count == 2 and sleep.delays == [3.0]


async def test_images_become_content_parts_and_effort_is_validated(llm: AicreditsLLM) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/chat/completions").mock(
            return_value=httpx.Response(
                200, json=completion({"stance": "neutral", "confidence": 0, "quote": None})
            )
        )
        await llm.complete_json(
            task="describe",
            system="s",
            user="what is this?",
            schema=Stance,
            effort="low",
            images=["data:image/png;base64,iVBORw0KGgo=", "https://example.org/a.jpg"],
        )
    body = sent_json(route)
    assert body["reasoning_effort"] == "low"
    assert body["messages"][1]["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        {"type": "image_url", "image_url": {"url": "https://example.org/a.jpg"}},
    ]
    with pytest.raises(ValueError):
        await llm.complete_json(task="t", system="s", user="u", schema=Stance, effort="turbo")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await llm.complete_json(task="t", system="s", user="u", schema=Stance, images=["file:///etc/passwd"])


async def test_llm_cassette_never_contains_the_key(make_settings) -> None:
    register_secrets([KEY])
    s = make_settings(llm_provider="aicredits", llm_api_key=KEY, tool_mode="record")
    tools = ToolClient(s, cache=ToolCache())
    client = AicreditsLLM(s, tools, AicreditsAPI(s, tools.http), ModelRegistry([MODEL]), model_id=MODEL)
    with respx.mock(base_url=BASE) as mock:
        mock.post("/chat/completions").mock(
            return_value=httpx.Response(
                200, json=completion({"stance": "support", "confidence": 1, "quote": None}), headers=HEADERS
            )
        )
        await client.complete_json(task="stance", system="s", user="u", schema=Stance)
    files = list((s.cassette_dir / "llm_primary").glob("*.json"))
    assert len(files) == 1 and KEY not in files[0].read_text(encoding="utf-8")
    await tools.aclose()


def test_non_allowlisted_model_cannot_be_constructed(llm_settings) -> None:
    tools = ToolClient(llm_settings)
    with pytest.raises(ModelNotAllowed):
        AicreditsLLM(
            llm_settings,
            tools,
            AicreditsAPI(llm_settings, tools.http),
            ModelRegistry([MODEL]),
            model_id="openai/gpt-4o",
        )


async def test_disabled_llm() -> None:
    llm = DisabledLLM()
    assert (llm.enabled, llm.provider, llm.model_id) == (False, "none", "none")
    with pytest.raises(LLMDisabled):
        await llm.complete_json(task="t", system="s", user="u", schema=Stance)


async def test_fake_llm_scripting(fake_llm: FakeLLM) -> None:
    fake_llm.script("a", {"stance": "support", "confidence": 0.5, "quote": None})
    fake_llm.script("b", Stance(stance="refute", confidence=0.1))
    fake_llm.script("c", lambda call: {"stance": call.user, "confidence": 0.0})
    fake_llm.script("d", [Stance(stance="x", confidence=0), RuntimeError("boom")])
    assert (await fake_llm.complete_json(task="a", system="", user="", schema=Stance)).stance == "support"
    assert (await fake_llm.complete_json(task="b", system="", user="", schema=Stance)).stance == "refute"
    assert (await fake_llm.complete_json(task="c", system="", user="echo", schema=Stance)).stance == "echo"
    assert (await fake_llm.complete_json(task="d", system="", user="", schema=Stance)).stance == "x"
    with pytest.raises(RuntimeError):
        await fake_llm.complete_json(task="d", system="", user="", schema=Stance)
    with pytest.raises(ToolCallFailed):
        await fake_llm.complete_json(task="unscripted", system="", user="", schema=Stance)
    assert [c.task for c in fake_llm.calls] == ["a", "b", "c", "d", "d", "unscripted"]
    assert (fake_llm.enabled, fake_llm.provider, fake_llm.model_id) == (True, "fake", "fake-model")


# ---- boot-time resolution (bootstrap.build_llm_clients) ----------------------------------------
MODELS = {
    "object": "list",
    "data": [
        {
            "id": "openai/gpt-6-luna",
            "is_active": True,
            "supported_apis": ["chat_completions"],
            "created": 1700000000,
        },
        {
            "id": "openai/gpt-5.6-luna",
            "is_active": True,
            "supported_apis": ["chat_completions"],
            "created": 1700000000,
        },
        {"id": "sao10k/l3-lunaris-8b", "is_active": True, "created": 1700000000},
    ],
}


async def test_resolution_success_builds_both_judges(llm_settings, llm_tools) -> None:
    tools, _ = llm_tools
    with respx.mock(base_url=BASE) as mock:
        mock.get("/models").mock(return_value=httpx.Response(200, json=MODELS))
        probe = mock.post("/chat/completions").mock(return_value=httpx.Response(200, json=completion("ok")))
        llm, llm2, resolved = await build_llm_clients(llm_settings, tools, None)
    assert resolved == {"llm_primary": "openai/gpt-6-luna", "llm_secondary": "openai/gpt-5.6-luna"}
    assert llm.model_id == "openai/gpt-6-luna" and llm2 is not None and llm2.model_id == "openai/gpt-5.6-luna"
    assert llm2.tool == "llm_secondary"
    probe_body = sent_json(probe)
    assert probe_body["model"] == "openai/gpt-6-luna" and probe_body["reasoning_effort"] == "none"
    assert tools.tool_versions()["llm_primary"] == "openai/gpt-6-luna"


async def test_resolution_fails_fast_on_bad_key(llm_settings, llm_tools) -> None:
    tools, _ = llm_tools
    with respx.mock(base_url=BASE) as mock:
        mock.get("/models").mock(return_value=httpx.Response(200, json=MODELS))  # listing ignores the key
        probe = mock.post("/chat/completions").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}},
            )
        )
        with structlog.testing.capture_logs() as logs, pytest.raises(ModelResolutionError) as exc_info:
            await build_llm_clients(llm_settings, tools, None)
    assert probe.call_count == 1 and "Invalid API Key" in str(exc_info.value)
    assert any(e["event"] == "model_resolution_failed" for e in logs)


async def test_resolution_fails_on_dns_error_and_missing_model(llm_settings, llm_tools) -> None:
    tools, _ = llm_tools
    with respx.mock(base_url=BASE) as mock:
        mock.get("/models").mock(side_effect=httpx.ConnectError("getaddrinfo failed"))
        with pytest.raises(ModelResolutionError, match="ConnectError"):
            await build_llm_clients(llm_settings, tools, None)
    tools.breaker("llm_primary").record_success()
    with respx.mock(base_url=BASE) as mock:
        mock.get("/models").mock(return_value=httpx.Response(200, json={"data": [MODELS["data"][2]]}))
        with pytest.raises(ModelResolutionError, match="no active listed model"):
            await build_llm_clients(llm_settings, tools, None)


async def test_provider_none_gives_disabled_llm(settings, tool_client) -> None:
    llm, llm2, resolved = await build_llm_clients(settings, tool_client, None)
    assert isinstance(llm, DisabledLLM) and llm2 is None and resolved == {"llm_primary": "none"}
    assert tool_client.status()["llm_primary"].state is ToolState.DISABLED
