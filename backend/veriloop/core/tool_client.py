"""ToolClient: the "flight recorder" every external call goes through (D-12, G3).

Order of checks for `call(tool, op, key, fn)`:
  1. unknown tool name            -> ValueError (programming error)
  2. disabled / unconfigured      -> ToolError("unconfigured"), fn never called (skipped in replay mode,
                                     where a recorded real response may be replayed without the key)
  3. chaos injection              -> ToolError("timeout" | "chaos"), fn never called
  4. TOOL_MODE=replay             -> cassette hit (replayed=True) or ToolError("replay_miss"); no network
  5. cache (memory + tool_cache)  -> from_cache=True
  6. circuit breaker              -> ToolError("circuit_open") while open; one probe when half-open
  7. attempts                     -> per-attempt timeout, retry x`retries` with exp backoff + jitter
                                     honouring Retry-After/X-Ratelimit-Reset; transient failures feed
                                     the breaker
  8. success                      -> cache store, cassette write in TOOL_MODE=record (secrets redacted)

Contract for `fn`: an argument-less coroutine function returning a raw JSON-serializable payload
(dict/list/str/number). Parse it in service code *after* the call so record/replay and the cache
return exactly what the live call returned. The `key` dict identifies the request for cache and
cassettes: include every input that changes the response, and never a secret.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

from veriloop.contracts import ToolError, ToolState, ToolStatusView
from veriloop.core.cache import ToolCache
from veriloop.core.chaos import ChaosState
from veriloop.core.config import Settings
from veriloop.core.errors import ToolAttemptError, ToolCallFailed
from veriloop.core.hashing import canonical_json, normalize_for_json, sha256_hex
from veriloop.core.http import create_http_client, error_for_response
from veriloop.core.logging import redact, redact_text
from veriloop.core.resilience import BreakerState, CircuitBreaker, Clock, RetryPolicy
from veriloop.core.tools_registry import ToolSpec, load_registry

log = structlog.get_logger(__name__)

ToolFn = Callable[[], Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[None]]

_MAX_MESSAGE = 500
_MAX_CASSETTE_KEY_STRING = 2000  # big strings (data URLs) are hashed, not stored, in the cassette key


@dataclass
class ToolCallResult:
    tool: str
    op: str
    ok: bool
    value: Any = None
    error: ToolError | None = None
    from_cache: bool = False
    replayed: bool = False
    latency_ms: int = 0
    attempts: int = 0

    def unwrap(self) -> Any:
        if not self.ok:
            assert self.error is not None
            raise ToolCallFailed(self.error)
        return self.value


@dataclass
class _ToolStats:
    calls: int = 0
    failures: int = 0
    last_error: str | None = None
    last_error_type: str | None = None
    last_ok: bool | None = None
    last_rate_limited_at: float | None = None


def call_key_hash(op: str, key: Mapping[str, Any]) -> str:
    """sha256(op + ":" + canonical_json(key)): the cache key and the cassette file name."""
    return sha256_hex(f"{op}:{canonical_json(key)}")


def classify_exception(exc: BaseException, timeout_s: float) -> ToolAttemptError:
    """Map anything a tool fn may raise to a classified attempt error."""
    if isinstance(exc, ToolAttemptError):
        return exc
    if isinstance(exc, ToolCallFailed):  # a nested tool call inside fn already failed and was logged
        return ToolAttemptError(exc.error.error_type, exc.error.message)
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return ToolAttemptError("timeout", f"no response within {timeout_s:g}s", retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        return error_for_response(exc.response)
    if isinstance(exc, httpx.TransportError):
        return ToolAttemptError("http_error", f"{type(exc).__name__}: {exc}", retryable=True)
    if isinstance(exc, ValueError):  # JSONDecodeError, pydantic ValidationError, bad payloads
        return ToolAttemptError("parse_error", f"{type(exc).__name__}: {exc}")
    return ToolAttemptError("exception", f"{type(exc).__name__}: {exc}")


def _shrink(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_CASSETTE_KEY_STRING:
        return f"<sha256:{sha256_hex(value)} len={len(value)}>"
    if isinstance(value, dict):
        return {k: _shrink(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink(v) for v in value]
    return value


class ToolClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        cache: ToolCache | None = None,
        chaos: ChaosState | None = None,
        registry: Mapping[str, ToolSpec] | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Clock = time.monotonic,
        sleep: SleepFn = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings
        self.http = http or create_http_client(settings)
        self.cache = cache or ToolCache(clock=clock)
        self.chaos = chaos or ChaosState()
        self.registry: dict[str, ToolSpec] = dict(
            registry
            if registry is not None
            else load_registry(settings.tools_config, llm_timeout_s=settings.llm_timeout_s)
        )
        self.retry_policy = retry_policy or RetryPolicy()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._breakers = {name: CircuitBreaker(clock=clock) for name in self.registry}
        self._stats = {name: _ToolStats() for name in self.registry}
        self._disabled: dict[str, str] = {}
        self._versions: dict[str, str] = {name: spec.version for name, spec in self.registry.items()}

    # ---- metadata ----------------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return self.settings.tool_mode

    def spec(self, tool: str) -> ToolSpec:
        try:
            return self.registry[tool]
        except KeyError:
            raise ValueError(f"unknown tool {tool!r}; register it in core/tools_registry.py") from None

    def missing_settings(self, tool: str) -> list[str]:
        return [name for name in self.spec(tool).required_settings if not self.settings.is_set(name)]

    def is_configured(self, tool: str) -> bool:
        return tool not in self._disabled and not self.missing_settings(tool)

    def disable(self, tool: str, reason: str) -> None:
        """Mark a tool deliberately off (e.g. llm_* when LLM_PROVIDER=none). Calls fail as unconfigured."""
        self.spec(tool)
        self._disabled[tool] = reason

    def enable(self, tool: str) -> None:
        self._disabled.pop(tool, None)

    def set_version(self, tool: str, version: str) -> None:
        self.spec(tool)
        self._versions[tool] = version

    def tool_versions(self) -> dict[str, str]:
        """tool -> version string, for audit payloads (G7)."""
        return dict(self._versions)

    def breaker(self, tool: str) -> CircuitBreaker:
        self.spec(tool)
        return self._breakers[tool]

    def secondary_for(self, tool: str) -> list[str]:
        """Configured fallback tools whose breaker is not open, in preference order."""
        return [
            name
            for name in self.spec(tool).secondary
            if self.is_configured(name)
            and self.chaos.get(name) == "off"
            and self._breakers[name].state is not BreakerState.OPEN
        ]

    def rate_limited_tools(self, within_s: float = 60.0) -> list[str]:
        """Tools rate-limited recently: lets the job runner show "processing (rate-limited: X)" (E10)."""
        now = self._clock()
        return [
            name
            for name, st in self._stats.items()
            if st.last_rate_limited_at is not None and now - st.last_rate_limited_at <= within_s
        ]

    def status(self) -> dict[str, ToolStatusView]:
        """Per-tool state for /health: keyless tools report ok/degraded, missing keys -> unconfigured."""
        out: dict[str, ToolStatusView] = {}
        for name in self.registry:
            st = self._stats[name]
            breaker_state = self._breakers[name].state
            configured = self.is_configured(name)
            if self.chaos.get(name) != "off":
                state = ToolState.CHAOS
            elif name in self._disabled:
                state = ToolState.DISABLED
            elif not configured:
                state = ToolState.UNCONFIGURED
            elif breaker_state is BreakerState.OPEN:
                state = ToolState.OPEN
            elif breaker_state is BreakerState.HALF_OPEN or st.last_ok is False:
                state = ToolState.DEGRADED
            else:
                state = ToolState.OK
            last_error = st.last_error
            if name in self._disabled:
                last_error = last_error or self._disabled[name]
            elif not configured:
                last_error = last_error or "missing " + ", ".join(
                    s.upper() for s in self.missing_settings(name)
                )
            out[name] = ToolStatusView(
                configured=configured,
                state=state,
                version=self._versions.get(name, ""),
                calls=st.calls,
                failures=st.failures,
                last_error=last_error,
            )
        return out

    # ---- calls -------------------------------------------------------------------------------
    async def call_or_raise(
        self,
        tool: str,
        op: str,
        key: Mapping[str, Any],
        fn: ToolFn,
        *,
        cache_ttl_s: int | None = None,
        timeout_s: float | None = None,
        retries: int | None = None,
    ) -> Any:
        """Like `call`, but returns the value or raises `ToolCallFailed(error)`."""
        result = await self.call(
            tool, op, key, fn, cache_ttl_s=cache_ttl_s, timeout_s=timeout_s, retries=retries
        )
        return result.unwrap()

    async def call(
        self,
        tool: str,
        op: str,
        key: Mapping[str, Any],
        fn: ToolFn,
        *,
        cache_ttl_s: int | None = None,
        timeout_s: float | None = None,
        retries: int | None = None,
    ) -> ToolCallResult:
        spec = self.spec(tool)
        started = time.perf_counter()
        stats = self._stats[tool]
        replay = self.mode == "replay"

        if tool in self._disabled:
            return self._fail(
                tool, op, "unconfigured", f"disabled: {self._disabled[tool]}", 0, started, count=False
            )
        missing = self.missing_settings(tool)
        if missing and not replay:
            msg = "missing setting(s): " + ", ".join(m.upper() for m in missing)
            return self._fail(tool, op, "unconfigured", msg, 0, started, count=False)

        stats.calls += 1
        chaos = self.chaos.get(tool)
        if chaos != "off":
            error_type = "timeout" if chaos == "timeout" else "chaos"
            return self._fail(tool, op, error_type, f"chaos: injected {chaos}", 1, started)

        key_hash = call_key_hash(op, key)
        if replay:
            return self._replay(tool, op, key_hash, started)

        ttl = spec.cache_ttl_s if cache_ttl_s is None else cache_ttl_s
        if ttl > 0:
            hit, cached = await self.cache.get(tool, key_hash)
            if hit:
                stats.last_ok = True
                return ToolCallResult(tool, op, True, cached, from_cache=True, latency_ms=_ms(started))

        breaker = self._breakers[tool]
        half_open = breaker.state is BreakerState.HALF_OPEN
        if not breaker.allow():
            msg = f"circuit open after repeated failures; retry in {breaker.retry_in_s():.0f}s"
            return self._fail(tool, op, "circuit_open", msg, 0, started)

        timeout = timeout_s if timeout_s is not None else spec.timeout_s
        # A half-open probe is a single attempt: its only job is to learn whether the tool is back.
        max_retries = 0 if half_open else (spec.retries if retries is None else retries)
        attempts = 0
        while True:
            attempts += 1
            try:
                async with asyncio.timeout(timeout):
                    raw = await fn()
                value = self._to_json(raw)
            except asyncio.CancelledError:
                if half_open:
                    breaker.release_probe()
                raise
            except Exception as exc:
                err = classify_exception(exc, timeout)
                if err.error_type == "rate_limited":
                    stats.last_rate_limited_at = self._clock()
                if err.retryable and attempts <= max_retries and breaker.state is BreakerState.CLOSED:
                    delay = self.retry_policy.delay(attempts, retry_after_s=err.retry_after_s, rng=self._rng)
                    if delay is not None:
                        log.info(
                            "tool_call_retry",
                            tool=tool,
                            op=op,
                            attempt=attempts,
                            error_type=err.error_type,
                            message=redact_text(err.message)[:_MAX_MESSAGE],
                            delay_s=round(delay, 3),
                        )
                        await self._sleep(delay)
                        continue
                # Only transient failures say "the service is unhealthy"; a 4xx means it answered.
                if err.retryable:
                    breaker.record_failure()
                else:
                    breaker.record_success()
                return self._fail(tool, op, err.error_type, err.message, attempts, started)

            breaker.record_success()
            stats.last_ok = True
            if ttl > 0:
                await self.cache.set(tool, key_hash, value, ttl, op=op)
            if self.mode == "record":
                self._record(tool, op, key, key_hash, value)
            return ToolCallResult(tool, op, True, value, latency_ms=_ms(started), attempts=attempts)

    # ---- internals ---------------------------------------------------------------------------
    @staticmethod
    def _to_json(raw: Any) -> Any:
        # Round-trip through JSON so live, cached and replayed values are indistinguishable.
        try:
            return json.loads(json.dumps(normalize_for_json(raw), ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ToolAttemptError("parse_error", f"tool fn returned a non-JSON value: {exc}") from exc

    def _fail(
        self,
        tool: str,
        op: str,
        error_type: str,
        message: str,
        attempts: int,
        started: float,
        *,
        count: bool = True,
    ) -> ToolCallResult:
        message = redact_text(message)[:_MAX_MESSAGE]
        error = ToolError(tool=tool, op=op, error_type=error_type, message=message, attempts=max(attempts, 1))
        latency = _ms(started)
        if count:
            st = self._stats[tool]
            st.failures += 1
            st.last_error = f"{error_type}: {message}"
            st.last_error_type = error_type
            st.last_ok = False
        level = log.info if error_type == "unconfigured" else log.warning
        level(
            "tool_call_failed",
            tool=tool,
            op=op,
            error_type=error_type,
            message=message,
            attempts=attempts,
            latency_ms=latency,
            mode=self.mode,
        )
        return ToolCallResult(tool, op, False, None, error, latency_ms=latency, attempts=attempts)

    def cassette_path(self, tool: str, op: str, key: Mapping[str, Any]) -> Path:
        return self._cassette_file(tool, call_key_hash(op, key))

    def _cassette_file(self, tool: str, key_hash: str) -> Path:
        return Path(self.settings.cassette_dir) / tool / f"{key_hash}.json"

    def _replay(self, tool: str, op: str, key_hash: str, started: float) -> ToolCallResult:
        path = self._cassette_file(tool, key_hash)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data["value"]
        except FileNotFoundError:
            return self._fail(tool, op, "replay_miss", f"no cassette {tool}/{path.name}", 1, started)
        except (OSError, ValueError, KeyError) as exc:
            return self._fail(
                tool, op, "replay_miss", f"unreadable cassette {tool}/{path.name}: {exc}", 1, started
            )
        self._stats[tool].last_ok = True
        return ToolCallResult(tool, op, True, value, replayed=True, latency_ms=_ms(started), attempts=1)

    def _record(self, tool: str, op: str, key: Mapping[str, Any], key_hash: str, value: Any) -> None:
        path = self._cassette_file(tool, key_hash)
        doc = {
            "format": 1,
            "tool": tool,
            "op": op,
            "recorded_at": datetime.now(UTC).isoformat(),
            "key": redact(_shrink(normalize_for_json(key))),
            "value": redact(value),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("cassette_write_failed", tool=tool, op=op, path=str(path), error=str(exc))
            return
        log.debug("cassette_recorded", tool=tool, op=op, path=str(path))

    async def aclose(self) -> None:
        await self.http.aclose()


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
