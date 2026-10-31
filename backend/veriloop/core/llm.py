"""LLM clients (D-01, D-01a, D-01b, D-16).

`LLMClient.complete_json(...)` is the only LLM entry point services use. It sends an
OpenAI-compatible chat completion with a strict `json_schema` response_format derived from the
pydantic `schema`, validates the reply with pydantic, does one repair round-trip on a validation
failure, and raises `ToolCallFailed` (error_type parse_error | refusal | http_error | timeout | ...)
otherwise. Every HTTP call goes through ToolClient as tool `llm_primary`/`llm_secondary`, so the
circuit breaker, chaos injection, status and record/replay cassettes apply; the cassette key is the
request payload, which never contains the API key.

D-16: the LLM is never a source. This client deliberately has no web-search/plugin parameters;
callers must give it evidence text and require citations.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Literal, TypeVar, get_args

import structlog
from pydantic import BaseModel

from veriloop.contracts import ToolError
from veriloop.core.aicredits import AicreditsAPI
from veriloop.core.config import Settings
from veriloop.core.errors import LLMDisabled, ToolCallFailed
from veriloop.core.model_registry import ModelRegistry
from veriloop.core.strict_schema import schema_name, to_strict_schema
from veriloop.core.tool_client import ToolCallResult, ToolClient

log = structlog.get_logger(__name__)

Effort = Literal["max", "xhigh", "high", "medium", "low", "minimal", "none"]
EFFORTS: frozenset[str] = frozenset(get_args(Effort))
LLMTool = Literal["llm_primary", "llm_secondary"]
UserContent = str | list[dict[str, Any]]
M = TypeVar("M", bound=BaseModel)

REPAIR_PROMPT = (
    "Your previous reply did not validate against the required JSON schema: {error}\n"
    "Reply again with only the corrected JSON object."
)


class LLMClient(ABC):
    """Interface every LLM implementation (real, disabled, fake) provides."""

    enabled: bool = False
    provider: str = "none"
    model_id: str = "none"
    tool: str = "llm_primary"

    @abstractmethod
    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: UserContent,
        schema: type[M],
        effort: Effort = "none",
        max_tokens: int = 2048,
        images: Sequence[str] | None = None,
    ) -> M:
        """Return a validated `schema` instance. Raises LLMDisabled or ToolCallFailed."""


class DisabledLLM(LLMClient):
    """LLM_PROVIDER=none ("extractive mode", D-01b): every call raises LLMDisabled."""

    enabled = False
    provider = "none"
    model_id = "none"

    def __init__(self, tool: str = "llm_primary") -> None:
        self.tool = tool

    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: UserContent,
        schema: type[M],
        effort: Effort = "none",
        max_tokens: int = 2048,
        images: Sequence[str] | None = None,
    ) -> M:
        raise LLMDisabled(f"LLM disabled (LLM_PROVIDER=none); task {task!r} must use its extractive path")


def build_user_content(user: UserContent, images: Sequence[str] | None) -> UserContent:
    """Attach images (data: URLs or http(s) URLs) as OpenAI-style `image_url` content parts."""
    if not images:
        return user
    for url in images:
        if not (url.startswith("data:image/") or url.startswith(("http://", "https://"))):
            raise ValueError("images must be data:image/... or http(s) URLs")
    parts: list[dict[str, Any]] = [{"type": "text", "text": user}] if isinstance(user, str) else list(user)
    parts.extend({"type": "image_url", "image_url": {"url": url}} for url in images)
    return parts


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def parse_content[T: BaseModel](schema: type[T], content: str | None) -> T:
    """Validate the model's JSON text. Raises ValueError (incl. pydantic ValidationError) on failure."""
    if not content or not content.strip():
        raise ValueError("empty response content")
    return schema.model_validate_json(_strip_fences(content))


def _short_error(exc: Exception, limit: int = 600) -> str:
    return " ".join(str(exc).split())[:limit]


def _rejects_response_format(error: ToolError) -> bool:
    msg = error.message.lower()
    return (
        error.error_type == "http_error"
        and "http 4" in msg
        and ("response_format" in msg or "json_schema" in msg)
    )


class AicreditsLLM(LLMClient):
    provider = "aicredits"
    enabled = True

    def __init__(
        self,
        settings: Settings,
        tools: ToolClient,
        api: AicreditsAPI,
        registry: ModelRegistry,
        *,
        model_id: str,
        tool: LLMTool = "llm_primary",
    ) -> None:
        registry.assert_allowed(model_id)  # never construct a client for a non-allowlisted id
        self.model_id = model_id
        self.tool = tool
        self._settings = settings
        self._tools = tools
        self._api = api
        self._registry = registry

    async def complete_json(
        self,
        *,
        task: str,
        system: str,
        user: UserContent,
        schema: type[M],
        effort: Effort = "none",
        max_tokens: int = 2048,
        images: Sequence[str] | None = None,
    ) -> M:
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {sorted(EFFORTS)}, got {effort!r}")
        self._registry.assert_allowed(self.model_id)
        strict = to_strict_schema(schema)
        op = f"chat:{task}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": build_user_content(user, images)},
        ]
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name(task), "strict": True, "schema": strict},
        }
        try:
            content = await self._chat(task, op, messages, response_format, effort, max_tokens)
        except ToolCallFailed as exc:
            if not _rejects_response_format(exc.error):
                raise
            # D-01a fallback: plain JSON mode with the schema spelled out in the system prompt.
            log.warning("llm_response_format_fallback", tool=self.tool, task=task, error=exc.error.message)
            op = f"chat:{task}:json_object"
            response_format = {"type": "json_object"}
            messages[0] = {
                "role": "system",
                "content": f"{system}\n\nRespond with one JSON object matching this JSON schema:\n"
                + json.dumps(strict, ensure_ascii=False),
            }
            content = await self._chat(task, op, messages, response_format, effort, max_tokens)

        try:
            return parse_content(schema, content)
        except ValueError as exc:
            first_error = _short_error(exc)
        log.warning("llm_parse_failed", tool=self.tool, task=task, model=self.model_id, error=first_error)
        repair = [
            *messages,
            {"role": "assistant", "content": content or ""},
            {"role": "user", "content": REPAIR_PROMPT.format(error=first_error)},
        ]
        content = await self._chat(task, f"{op}:repair", repair, response_format, effort, max_tokens)
        try:
            return parse_content(schema, content)
        except ValueError as exc:
            raise self._failure(
                op, "parse_error", f"invalid JSON after repair: {_short_error(exc)}", 2
            ) from exc

    async def _chat(
        self,
        task: str,
        op: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any],
        effort: str,
        max_tokens: int,
    ) -> str | None:
        payload = {
            "model": self.model_id,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "reasoning_effort": effort,
            "response_format": response_format,
        }
        result = await self._tools.call(
            self.tool,
            op,
            payload,
            lambda: self._api.chat(payload),
            cache_ttl_s=0,
            timeout_s=self._settings.llm_timeout_s,
        )
        if not result.ok:
            assert result.error is not None
            raise ToolCallFailed(result.error)
        body = (result.value or {}).get("body") or {}
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise self._failure(op, "parse_error", "response has no choices", result.attempts)
        choice = choices[0]
        message = choice.get("message") or {}
        self._log_usage(task, op, effort, result, body, choice)
        refusal = message.get("refusal")
        if refusal:
            raise self._failure(op, "refusal", str(refusal), result.attempts)
        content = message.get("content")
        if isinstance(content, list):  # some upstreams return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content

    def _failure(self, op: str, error_type: str, message: str, attempts: int) -> ToolCallFailed:
        error = ToolError(
            tool=self.tool, op=op, error_type=error_type, message=message[:500], attempts=max(1, attempts)
        )
        log.warning(
            "tool_call_failed",
            tool=self.tool,
            op=op,
            error_type=error_type,
            message=error.message,
            model=self.model_id,
        )
        return ToolCallFailed(error)

    def _log_usage(
        self,
        task: str,
        op: str,
        effort: str,
        result: ToolCallResult,
        body: dict[str, Any],
        choice: dict[str, Any],
    ) -> None:
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        headers = (result.value or {}).get("headers") or {}
        log.info(
            "llm_call",
            tool=self.tool,
            task=task,
            op=op,
            model=self.model_id,
            effort=effort,
            finish_reason=choice.get("finish_reason"),
            native_finish_reason=choice.get("native_finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            reasoning_tokens=details.get("reasoning_tokens"),
            cost_inr=usage.get("cost", headers.get("x-aicredits-cost-inr")),
            provider_latency_ms=usage.get("latency_ms"),
            latency_ms=result.latency_ms,
            attempts=result.attempts,
            request_id=headers.get("x-request-id"),
            ratelimit_remaining=headers.get("x-ratelimit-remaining"),
            replayed=result.replayed,
        )
