"""Per-app runtime objects (Deps, job runner, Telegram service) and the accessor routes use."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from veriloop.core.errors import ProblemError
from veriloop.deps import Deps
from veriloop.orchestrator import JobRunner


@dataclass
class AppRuntime:
    deps: Deps
    runner: JobRunner
    telegram: Any = None  # bots.telegram.TelegramService when TELEGRAM_BOT_TOKEN is set and it started
    prewarm: dict[str, str] = field(default_factory=dict)  # module -> ok|no prewarm hook|failed: ...
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)


def get_runtime(request: Request) -> AppRuntime:
    runtime: AppRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # lifespan not run (or still starting): answer instead of crashing
        raise ProblemError(503, "Service starting", "The server is still starting up; retry in a moment.")
    return runtime
