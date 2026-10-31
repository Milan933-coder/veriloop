"""FastAPI app factory (BUILD_SPEC §9, SPEC §10). `python -m veriloop` runs `create_app()` via uvicorn.

Lifespan: bootstrap.startup (fails fast on LLM model resolution, D-01) -> job runner (marks jobs a
previous process left unfinished as failed) -> background tasks: Telegram bot when configured,
TTL purge loop every 10 min (G6/D7), prewarm of service packages that expose `prewarm(deps)` when
ML_PREWARM=true -> serve -> stop everything -> bootstrap.shutdown.

Middleware (outermost first): request context (X-Request-Id, latency_ms in every JSON body, 500 as
problem+json) -> CORS for the Vite dev server -> routes. Errors are RFC 7807 problem+json. API routes
take precedence over the SPA (frontend/dist) which is mounted last with an index.html fallback.

Tests pass `deps` (and a fake `services` ServiceSet): the lifespan then skips bootstrap.startup /
shutdown and the Telegram/purge/prewarm background tasks unless asked for.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address

from veriloop import __version__
from veriloop.bootstrap import shutdown, startup
from veriloop.core.config import Settings, get_settings
from veriloop.deps import Deps
from veriloop.orchestrator import JobRunner, ServiceSet, default_services, prewarm_services

from .context import LATENCY_HEADER, REQUEST_ID_HEADER, RequestContextMiddleware
from .problems import install_problem_handlers
from .routes import actions, audit, system, verify
from .runtime import AppRuntime
from .spa import DEFAULT_DIST, mount_spa

log = structlog.get_logger(__name__)

DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def keep_temp_files_on_home(settings: Settings) -> Path:
    """Point Python's tempfile (multipart spooling, libraries) and child processes at VERILOOP_HOME/tmp.

    C: is nearly full and BUILD_SPEC forbids writing runtime data there; the default %TEMP% is on C:.
    """
    tmp = settings.home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(tmp)
    for var in ("TMP", "TEMP", "TMPDIR"):
        os.environ[var] = str(tmp)
    return tmp


async def _start_telegram(runtime: AppRuntime) -> None:
    try:
        from veriloop.bots.telegram import start_telegram

        runtime.telegram = await start_telegram(runtime.deps, runtime.runner.submit, runtime.runner.wait)
    except Exception as exc:  # the API must stay up even if the bot cannot start
        log.error("telegram_start_error", error=str(exc)[:300])


async def _purge_loop(deps: Deps) -> None:
    from veriloop.bots.telegram import run_purge_loop

    await run_purge_loop(deps)


async def _prewarm(runtime: AppRuntime) -> None:
    runtime.prewarm.update(await prewarm_services(runtime.deps))
    log.info("prewarm_finished", results=runtime.prewarm)


def create_app(
    *,
    deps: Deps | None = None,
    services: ServiceSet | None = None,
    settings: Settings | None = None,
    frontend_dist: Path | None = DEFAULT_DIST,
    background: bool | None = None,
) -> FastAPI:
    """Build the app. `deps`: use these instead of bootstrap.startup (tests); `background`: run the
    Telegram / purge / prewarm tasks (default: only when the app owns its Deps)."""
    settings = deps.settings if deps is not None else (settings or get_settings())
    owns_deps = deps is None
    run_background = owns_deps if background is None else background

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_deps:
            tmp = keep_temp_files_on_home(settings)
            log.info("temp_dir_redirected", path=str(tmp))
        app_deps = deps if deps is not None else await startup(settings)
        runner = JobRunner(app_deps, services or default_services())
        runtime = AppRuntime(deps=app_deps, runner=runner)
        try:
            await runner.start()
            if run_background:
                runtime.tasks.append(asyncio.create_task(_start_telegram(runtime), name="telegram-start"))
                runtime.tasks.append(asyncio.create_task(_purge_loop(app_deps), name="ttl-purge"))
                if settings.ml_prewarm:
                    runtime.tasks.append(asyncio.create_task(_prewarm(runtime), name="prewarm"))
            app.state.runtime = runtime
            log.info(
                "api_ready",
                version=__version__,
                base_url=settings.app_base_url,
                max_concurrent_jobs=runner.max_concurrent,
                background=run_background,
                api_keys=bool(settings.api_key_list),
                rate_limit_per_min=settings.rate_limit_per_min,
            )
            yield
        finally:
            app.state.runtime = None
            for task in runtime.tasks:
                task.cancel()
            await asyncio.gather(*runtime.tasks, return_exceptions=True)
            if runtime.telegram is not None:
                try:
                    await runtime.telegram.stop()
                except Exception as exc:
                    log.warning("telegram_stop_failed", error=str(exc)[:300])
            await runner.shutdown()
            if owns_deps:
                await shutdown(app_deps)

    app = FastAPI(
        title="VeriLoop API",
        version=__version__,
        description="Fact verification + deepfake audit with a human-gated enforcement loop. "
        "Every JSON body carries request_id and latency_ms; errors are application/problem+json.",
        lifespan=lifespan,
    )
    limiter = Limiter(key_func=get_remote_address, storage_uri="memory://", headers_enabled=False)
    app.state.limiter = limiter
    install_problem_handlers(app)

    app.include_router(system.router)
    app.include_router(verify.build_router(limiter, settings.rate_limit_per_min))
    app.include_router(actions.router)
    app.include_router(audit.router)
    mount_spa(app, frontend_dist)  # last: API routes win

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER, LATENCY_HEADER, "Location"],
    )
    app.add_middleware(RequestContextMiddleware)  # added last = outermost
    return app


def app_runtime(app: FastAPI) -> Any:
    """The running AppRuntime (None outside the lifespan); handy for scripts and tests."""
    return getattr(app.state, "runtime", None)
