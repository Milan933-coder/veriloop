"""LLM model resolution (D-01, SPEC rule D1: never hardcode a model id).

Boot sequence (`resolve_llm_models`): list models -> match the configured allowlist aliases ->
one tiny authenticated probe call to the resolved primary (the listing endpoint does not check
the key) -> return the ids. Persisting + logging `model_resolved` happens in bootstrap.

Matching rule: an id matches allowlist entry E when its last `/`-segment equals E or ends with
`-E` (`openai/gpt-5.6-luna` matches `5.6-luna`; `sao10k/l3-lunaris-8b` matches nothing). The
listing's `created` field is a constant placeholder, so it is never used for ordering.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from veriloop.core.aicredits import AicreditsAPI
from veriloop.core.config import Settings
from veriloop.core.errors import ModelNotAllowed, ModelResolutionError, ToolCallFailed
from veriloop.core.tool_client import ToolClient

log = structlog.get_logger(__name__)

PROBE_PROMPT = "Reply with the single word: ok"


def matches_allowlist_entry(model_id: str, entry: str) -> bool:
    last = model_id.strip().rsplit("/", 1)[-1].lower()
    entry = entry.strip().lower()
    return bool(entry) and (last == entry or last.endswith("-" + entry))


def _usable(model: Mapping[str, Any]) -> bool:
    if model.get("is_active") is False:
        return False
    apis = model.get("supported_apis")
    return not (isinstance(apis, list) and apis and "chat_completions" not in apis)


def resolve_alias(listing: Iterable[Mapping[str, Any]], entry: str) -> str | None:
    """Pick the listed id for one allowlist entry. Deterministic: exact last segment first, then shortest."""
    candidates = [
        m["id"]
        for m in listing
        if isinstance(m.get("id"), str) and _usable(m) and matches_allowlist_entry(m["id"], entry)
    ]
    if not candidates:
        return None
    exact = [c for c in candidates if c.rsplit("/", 1)[-1].lower() == entry.strip().lower()]
    pool = exact or candidates
    return sorted(pool, key=lambda c: (len(c), c))[0]


@dataclass(frozen=True)
class ResolvedModels:
    primary: str
    secondary: str | None
    primary_entry: str
    secondary_entry: str | None
    probe_latency_ms: int = 0
    candidates: dict[str, list[str]] = field(default_factory=dict)


def resolve_from_listing(listing: Sequence[Mapping[str, Any]], settings: Settings) -> ResolvedModels:
    allowed = settings.allowed_models
    if not allowed:
        raise ModelResolutionError("LLM_ALLOWED_MODELS is empty")
    for name, entry in (
        ("LLM_PRIMARY_MODEL", settings.llm_primary_model),
        ("LLM_SECONDARY_MODEL", settings.llm_secondary_model),
    ):
        if entry and entry not in allowed:
            raise ModelResolutionError(f"{name}={entry!r} is not in LLM_ALLOWED_MODELS={allowed}")
    candidates = {
        entry: sorted(
            m["id"]
            for m in listing
            if isinstance(m.get("id"), str) and matches_allowlist_entry(m["id"], entry)
        )
        for entry in allowed
    }
    primary = resolve_alias(listing, settings.llm_primary_model)
    if primary is None:
        raise ModelResolutionError(
            f"no active listed model matches LLM_PRIMARY_MODEL={settings.llm_primary_model!r} "
            f"({len(listing)} models listed)"
        )
    secondary = resolve_alias(listing, settings.llm_secondary_model) if settings.llm_secondary_model else None
    return ResolvedModels(
        primary,
        secondary,
        settings.llm_primary_model,
        settings.llm_secondary_model or None,
        candidates=candidates,
    )


class ModelRegistry:
    """The set of model ids this process may call: only ids resolved from the allowlist at boot."""

    def __init__(self, resolved_ids: Iterable[str]) -> None:
        self._allowed = frozenset(i for i in resolved_ids if i)

    @property
    def allowed_ids(self) -> frozenset[str]:
        return self._allowed

    def assert_allowed(self, model_id: str) -> None:
        if model_id not in self._allowed:
            raise ModelNotAllowed(f"model {model_id!r} was not resolved from LLM_ALLOWED_MODELS")


async def fetch_listing(settings: Settings, tools: ToolClient, api: AicreditsAPI) -> list[dict[str, Any]]:
    value = await tools.call_or_raise(
        "llm_primary", "list_models", {"base_url": api.base_url}, api.list_models, cache_ttl_s=0
    )
    return list(value.get("data") or [])


async def probe(settings: Settings, tools: ToolClient, api: AicreditsAPI, model_id: str) -> int:
    """Smallest authenticated call (~15 tokens, effort none). Returns latency in ms."""
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_completion_tokens": 16,
        "reasoning_effort": "none",
    }
    started = time.perf_counter()
    value = await tools.call_or_raise(
        "llm_primary",
        "probe",
        payload,
        lambda: api.chat(payload),
        cache_ttl_s=0,
        timeout_s=settings.llm_timeout_s,
    )
    choices = (value.get("body") or {}).get("choices")
    if not choices:
        raise ModelResolutionError(f"probe call to {model_id} returned no choices")
    return int((time.perf_counter() - started) * 1000)


async def resolve_llm_models(settings: Settings, tools: ToolClient, api: AicreditsAPI) -> ResolvedModels:
    """List -> match allowlist -> probe primary. Raises ModelResolutionError with the cause on any failure."""
    try:
        listing = await fetch_listing(settings, tools, api)
        resolved = resolve_from_listing(listing, settings)
        latency = await probe(settings, tools, api, resolved.primary)
    except ModelResolutionError:
        raise
    except ToolCallFailed as exc:
        raise ModelResolutionError(f"{exc.error.op}: {exc.error.error_type}: {exc.error.message}") from exc
    return ResolvedModels(
        resolved.primary,
        resolved.secondary,
        resolved.primary_entry,
        resolved.secondary_entry,
        probe_latency_ms=latency,
        candidates=resolved.candidates,
    )
