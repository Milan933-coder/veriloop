"""Process startup/shutdown: builds the `Deps` container every service receives.

`startup()` order: logging (+ secret redaction) -> dirs -> CPU pool -> DB (embedded pgserver when
DATABASE_URL is empty) + schema -> HTTP client + ToolClient -> LLM model resolution (fail fast,
D-01) -> lazy Embedder/NLI -> EventBus. A ModelResolutionError propagates so the ASGI lifespan
fails and the process exits non-zero; everything opened so far is closed first.
"""

from __future__ import annotations

from typing import Any

import structlog

from veriloop import __version__
from veriloop.contracts import HealthView
from veriloop.core.aicredits import AicreditsAPI
from veriloop.core.cache import ToolCache
from veriloop.core.chaos import ChaosState
from veriloop.core.config import Settings, get_settings
from veriloop.core.errors import ModelResolutionError
from veriloop.core.events import EventBus
from veriloop.core.executor import configure_executor, shutdown_executor
from veriloop.core.http import create_http_client
from veriloop.core.llm import AicreditsLLM, DisabledLLM, LLMClient
from veriloop.core.logging import configure_logging, register_secrets
from veriloop.core.ml import NLI, Embedder
from veriloop.core.model_registry import ModelRegistry, resolve_llm_models
from veriloop.core.tool_client import ToolClient
from veriloop.core.tools_registry import load_registry
from veriloop.db import repo
from veriloop.db.engine import Database
from veriloop.deps import Deps

log = structlog.get_logger(__name__)


async def build_llm_clients(
    settings: Settings, tools: ToolClient, db: Database | None
) -> tuple[LLMClient, LLMClient | None, dict[str, str]]:
    """Resolve allowlisted models and build `llm` / `llm2`. Raises ModelResolutionError (fatal)."""
    if not settings.llm_enabled:
        reason = "LLM_PROVIDER=none" if not settings.llm_provider_auto else "no LLM_API_KEY (auto -> none)"
        for tool in ("llm_primary", "llm_secondary"):
            tools.disable(tool, reason)
        log.warning("llm_disabled", mode="extractive", reason=reason)
        resolved = {"llm_primary": "none"}
        if db is not None:
            await repo.upsert_model(
                db, "llm_primary", provider="none", model_id="none", meta={"reason": reason}
            )
        return DisabledLLM(), None, resolved

    api = AicreditsAPI(settings, tools.http)
    try:
        models = await resolve_llm_models(settings, tools, api)
    except ModelResolutionError as exc:
        log.error(
            "model_resolution_failed",
            provider=settings.llm_provider,
            base_url=settings.llm_base_url,
            allowed=settings.allowed_models,
            cause=str(exc),
        )
        raise

    ids = [models.primary] + ([models.secondary] if models.secondary else [])
    registry = ModelRegistry(ids)
    llm = AicreditsLLM(settings, tools, api, registry, model_id=models.primary, tool="llm_primary")
    tools.set_version("llm_primary", models.primary)
    resolved = {"llm_primary": models.primary}

    llm2: LLMClient | None = None
    if not settings.llm_second_judge:
        tools.disable("llm_secondary", "LLM_SECOND_JUDGE=false")
    elif models.secondary is None:
        tools.disable("llm_secondary", f"no listed model matches {settings.llm_secondary_model!r}")
        log.warning("second_judge_unavailable", entry=settings.llm_secondary_model)
    else:
        llm2 = AicreditsLLM(settings, tools, api, registry, model_id=models.secondary, tool="llm_secondary")
        tools.set_version("llm_secondary", models.secondary)
        resolved["llm_secondary"] = models.secondary

    if db is not None:
        meta = {"base_url": settings.llm_base_url, "probe_latency_ms": models.probe_latency_ms}
        await repo.upsert_model(
            db,
            "llm_primary",
            provider="aicredits",
            model_id=models.primary,
            meta={**meta, "allowlist_entry": models.primary_entry},
        )
        if llm2 is not None:
            await repo.upsert_model(
                db,
                "llm_secondary",
                provider="aicredits",
                model_id=llm2.model_id,
                meta={**meta, "allowlist_entry": models.secondary_entry},
            )
    log.info(
        "model_resolved",
        provider="aicredits",
        **resolved,
        probe_latency_ms=models.probe_latency_ms,
        candidates=models.candidates,
    )
    return llm, llm2, resolved


async def startup(settings: Settings | None = None) -> Deps:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    register_secrets(settings.secret_values())
    settings.ensure_dirs()
    log.info(
        "startup_begin",
        version=__version__,
        tool_mode=settings.tool_mode,
        llm_provider=settings.llm_provider,
        llm_provider_auto=settings.llm_provider_auto,
        home=str(settings.home),
        database="external" if settings.database_url else "embedded",
        ml_enable=settings.ml_enable,
        labels=settings.mode_labels(),
    )
    configure_executor(settings.cpu_workers)

    db: Database | None = None
    tools: ToolClient | None = None
    try:
        db = await Database.connect(settings)
        await db.apply_schema()
        purged = await repo.purge_expired_cache(db)
        tools = ToolClient(
            settings,
            http=create_http_client(settings),
            cache=ToolCache(db),
            chaos=ChaosState(),
            registry=load_registry(settings.tools_config, llm_timeout_s=settings.llm_timeout_s),
        )
        llm, llm2, resolved = await build_llm_clients(settings, tools, db)
        deps = Deps(
            settings=settings,
            db=db,
            tools=tools,
            llm=llm,
            llm2=llm2,
            embedder=Embedder(settings),
            nli=NLI(settings),
            events=EventBus(),
            resolved_models=resolved,
        )
    except BaseException:
        if tools is not None:
            await tools.aclose()
        if db is not None:
            await db.close()
        raise
    status = tools.status()
    log.info(
        "startup_complete",
        resolved_models=resolved,
        cache_purged=purged,
        configured=sorted(t for t, s in status.items() if s.configured),
        unconfigured=sorted(t for t, s in status.items() if not s.configured),
    )
    return deps


async def shutdown(deps: Deps) -> None:
    deps.events.close_all()
    await deps.tools.aclose()
    await deps.db.close()
    shutdown_executor(wait=False)
    log.info("shutdown_complete")


async def build_health(deps: Deps) -> HealthView:
    """Snapshot for GET /health (the API engineer may reuse this as-is)."""
    db_ok = await deps.db.ping()
    tool_status = deps.tools.status()
    ml = {deps.embedder.model_id: deps.embedder.status(), deps.nli.model_id: deps.nli.status()}
    return HealthView(
        status="ok" if db_ok else "degraded",
        mode=deps.settings.tool_mode,
        version=__version__,
        resolved_models=deps.resolved_models,
        tool_status=tool_status,
        db="ok" if db_ok else "unreachable",
        ml=ml,
        llm_provider=deps.llm.provider,
    )


def build_deps_for_tests(
    settings: Settings,
    *,
    db: Database,
    tools: ToolClient | None = None,
    llm: LLMClient | None = None,
    llm2: LLMClient | None = None,
    embedder: Any = None,
    nli: Any = None,
    events: EventBus | None = None,
    resolved_models: dict[str, str] | None = None,
) -> Deps:
    """Assemble Deps from parts without network/model resolution (used by tests/conftest.py)."""
    llm = llm or DisabledLLM()
    return Deps(
        settings=settings,
        db=db,
        tools=tools or ToolClient(settings, cache=ToolCache()),
        llm=llm,
        llm2=llm2,
        embedder=embedder if embedder is not None else Embedder(settings),
        nli=nli if nli is not None else NLI(settings),
        events=events or EventBus(),
        resolved_models=resolved_models
        if resolved_models is not None
        else {"llm_primary": llm.model_id, **({"llm_secondary": llm2.model_id} if llm2 else {})},
    )
