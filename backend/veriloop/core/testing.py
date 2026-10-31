"""Test doubles shared by every package's tests (no network, no model downloads).

    from veriloop.core.testing import FakeLLM, FakeEmbedder, FakeNLI, FakeClock, RecordingSleep

FakeLLM scripting (per `task`): a pydantic instance, a dict (validated against the call's schema),
a callable `(call: LLMCall) -> instance | dict`, an Exception instance (raised), or a list of those
(consumed one per call; the last one repeats). Unscripted tasks raise ToolCallFailed like a real
outage would, so services exercise their failure path; inspect `fake.calls` to assert prompts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel

from veriloop.contracts import ToolError
from veriloop.core.errors import MLUnavailable, ToolCallFailed
from veriloop.core.llm import Effort, LLMClient, M, UserContent
from veriloop.core.ml import EMBED_DIM, NLIScore


@dataclass
class LLMCall:
    task: str
    system: str
    user: UserContent
    schema: type[BaseModel]
    effort: str
    max_tokens: int
    images: list[str] = field(default_factory=list)


class FakeLLM(LLMClient):
    enabled = True
    provider = "fake"

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        *,
        model_id: str = "fake-model",
        tool: str = "llm_primary",
    ) -> None:
        self.model_id = model_id
        self.tool = tool
        self._scripts: dict[str, Any] = dict(responses or {})
        self.calls: list[LLMCall] = []

    def script(self, task: str, response: Any) -> FakeLLM:
        self._scripts[task] = response
        return self

    def calls_for(self, task: str) -> list[LLMCall]:
        return [c for c in self.calls if c.task == task]

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
        call = LLMCall(task, system, user, schema, effort, max_tokens, list(images or []))
        self.calls.append(call)
        if task not in self._scripts:
            raise ToolCallFailed(
                ToolError(
                    tool=self.tool,
                    op=f"chat:{task}",
                    error_type="exception",
                    message=f"FakeLLM has no response scripted for task {task!r}",
                )
            )
        response = self._scripts[task]
        if isinstance(response, list):
            if not response:
                raise ValueError(f"FakeLLM script for {task!r} is an empty list")
            response = response.pop(0) if len(response) > 1 else response[0]
        if callable(response) and not isinstance(response, BaseModel | type):
            response = response(call)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, schema):
            return response
        if isinstance(response, BaseModel):
            return schema.model_validate(response.model_dump())
        return schema.model_validate(response)


class FakeEmbedder:
    """Deterministic pseudo-embeddings: identical texts -> identical unit vectors; no model."""

    model_id = "fake-embedder"
    dim = EMBED_DIM

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.calls: list[tuple[list[str], str]] = []

    @property
    def available(self) -> bool:
        return self._available

    def status(self) -> str:
        return "loaded" if self._available else "unavailable"

    async def load(self) -> bool:
        return self._available

    @staticmethod
    def vector_for(text: str, kind: str = "query") -> np.ndarray:
        # Prefix-free on purpose: query and passage of the same text map to the same vector, so tests
        # can assert "the matching passage ranks first".
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        vec = np.random.default_rng(seed).standard_normal(EMBED_DIM).astype(np.float32)
        return vec / np.linalg.norm(vec)

    async def embed(self, texts: Sequence[str], kind: Literal["query", "passage"] = "query") -> np.ndarray:
        if not self._available:
            raise MLUnavailable("FakeEmbedder configured unavailable")
        self.calls.append((list(texts), kind))
        if not texts:
            return np.zeros((0, EMBED_DIM), dtype=np.float32)
        return np.stack([self.vector_for(t, kind) for t in texts])


class FakeNLI:
    """Scriptable NLI: `fn(premise, hypothesis) -> NLIScore`; default is neutral."""

    model_id = "fake-nli"

    def __init__(
        self,
        fn: Callable[[str, str], NLIScore] | None = None,
        *,
        available: bool = True,
    ) -> None:
        self._fn = fn or (lambda _p, _h: NLIScore(entailment=0.1, neutral=0.8, contradiction=0.1))
        self._available = available
        self.calls: list[tuple[str, str]] = []

    @property
    def available(self) -> bool:
        return self._available

    def status(self) -> str:
        return "loaded" if self._available else "unavailable"

    async def load(self) -> bool:
        return self._available

    async def score(self, pairs: Sequence[tuple[str, str]]) -> list[NLIScore]:
        if not self._available:
            raise MLUnavailable("FakeNLI configured unavailable")
        self.calls.extend(pairs)
        return [self._fn(p, h) for p, h in pairs]


class FakeClock:
    """Monotonic clock for breaker/cache tests: `clock()` returns now, `advance(s)` moves time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Drop-in for asyncio.sleep that returns immediately, records delays and advances a FakeClock."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.delays: list[float] = []
        self._clock = clock

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self._clock is not None:
            self._clock.advance(delay)
