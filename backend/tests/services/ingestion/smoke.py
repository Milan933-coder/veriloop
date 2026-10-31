"""Shared setup for the media smoke tests (real .env settings, real ToolClient, real models on D:)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from veriloop.bootstrap import build_deps_for_tests, build_llm_clients
from veriloop.core.cache import ToolCache
from veriloop.core.config import Settings, get_settings
from veriloop.core.tool_client import ToolClient
from veriloop.db.engine import Database
from veriloop.deps import Deps


def live_settings(**overrides: Any) -> Settings:
    """The real settings (VERILOOP_HOME on D:, LLM key from .env) with optional overrides."""
    base = get_settings()
    settings = base.model_copy(update=overrides) if overrides else base
    settings.ensure_dirs()  # also points HF/torch/easyocr caches at models_dir before any import
    return settings


@asynccontextmanager
async def live_deps(db: Database, *, llm: bool = True, **overrides: Any) -> AsyncIterator[Deps]:
    settings = live_settings(**overrides)
    tools = ToolClient(settings, cache=ToolCache())
    try:
        if llm:
            primary, secondary, resolved = await build_llm_clients(settings, tools, None)
        else:
            from veriloop.core.llm import DisabledLLM

            primary, secondary, resolved = DisabledLLM(), None, {"llm_primary": "none"}
        yield build_deps_for_tests(
            settings, db=db, tools=tools, llm=primary, llm2=secondary, resolved_models=resolved
        )
    finally:
        await tools.aclose()


class Stopwatch:
    def __init__(self) -> None:
        self.marks: dict[str, float] = {}
        self._t = time.perf_counter()

    def lap(self, name: str) -> float:
        now = time.perf_counter()
        self.marks[name] = round(now - self._t, 2)
        self._t = now
        return self.marks[name]


def emit(name: str, report: dict[str, Any]) -> None:
    """Print the smoke report (ASCII-safe for the Windows console) and keep a copy under VERILOOP_HOME/tmp."""
    text = json.dumps(report, indent=2, default=str)
    out = live_settings().home / "tmp" / f"smoke_{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\nSMOKE {name} (saved to {out}):\n{text}")
