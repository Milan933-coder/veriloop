"""Indirection between the graph and the service packages (BUILD_SPEC §5).

The orchestrator never imports `veriloop.services.*` at module import time: `default_services()`
returns callables that import their package lazily on first use. A package that is missing, fails
to import, or lacks the function raises `ServiceUnavailable` *inside the node*, which the node
wrapper turns into an `error` node + `ToolError(tool="node:<name>")`, and the job still finishes
(UNVERIFIABLE / TOOL_OUTAGE, audited; E6). Tests pass their own `ServiceSet` of fakes.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# stage callable -> package that implements it (BUILD_SPEC §5)
SERVICE_MODULES: dict[str, str] = {
    "resolve_input": "veriloop.services.ingestion",
    "extract_claims": "veriloop.services.extraction",
    "gather_evidence": "veriloop.services.evidence",
    "followup_search": "veriloop.services.evidence",
    "run_forensics": "veriloop.services.forensic",
    "run_provenance": "veriloop.services.provenance",
    "fuse": "veriloop.services.fusion",
    "decide": "veriloop.services.fusion",
    "run_debate": "veriloop.services.debate",
}
_SYNC = frozenset({"fuse", "decide"})  # pure, deterministic, no I/O

# Packages whose optional `async def prewarm(deps)` the app calls at startup (ML_PREWARM=true).
PREWARM_MODULES: tuple[str, ...] = (
    "veriloop.services.ingestion",
    "veriloop.services.extraction",
    "veriloop.services.evidence",
    "veriloop.services.forensic",
    "veriloop.services.provenance",
    "veriloop.services.fusion",
    "veriloop.services.debate",
)


class ServiceUnavailable(RuntimeError):
    """A pipeline stage's implementation is missing or failed to import."""


def resolve_service(module: str, attr: str) -> Callable[..., Any]:
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        raise ServiceUnavailable(f"{module} is not available ({type(exc).__name__}: {exc})") from exc
    fn = getattr(mod, attr, None)
    if not callable(fn):
        raise ServiceUnavailable(f"{module}.{attr} is not implemented")
    return fn


async def resolve_service_async(module: str, attr: str) -> Callable[..., Any]:
    """Like `resolve_service`, but a first-time import runs in a worker thread.

    Some packages import torch/open_clip at module level (the forensic package takes ~11 s); doing
    that on the event loop would freeze every SSE stream and request for the duration.
    """
    if module in sys.modules:
        return resolve_service(module, attr)
    return await asyncio.to_thread(resolve_service, module, attr)


def _lazy(name: str) -> Callable[..., Any]:
    module = SERVICE_MODULES[name]
    if name in _SYNC:

        def call_sync(*args: Any, **kwargs: Any) -> Any:
            return resolve_service(module, name)(*args, **kwargs)

        fn: Callable[..., Any] = call_sync
    else:

        async def call_async(*args: Any, **kwargs: Any) -> Any:
            return await (await resolve_service_async(module, name))(*args, **kwargs)

        fn = call_async
    fn.__name__ = name
    fn.__qualname__ = f"lazy:{module}.{name}"
    return fn


AsyncFn = Callable[..., Awaitable[Any]]
SyncFn = Callable[..., Any]


@dataclass
class ServiceSet:
    """The BUILD_SPEC §5 callables the graph uses. `None` means "not available" (node error)."""

    resolve_input: AsyncFn | None = None
    extract_claims: AsyncFn | None = None
    gather_evidence: AsyncFn | None = None
    followup_search: AsyncFn | None = None
    run_forensics: AsyncFn | None = None
    run_provenance: AsyncFn | None = None
    fuse: SyncFn | None = None
    decide: SyncFn | None = None
    run_debate: AsyncFn | None = None

    def get(self, name: str) -> Callable[..., Any]:
        fn = getattr(self, name, None)
        if fn is None:
            raise ServiceUnavailable(f"service {name!r} is not available in this ServiceSet")
        return fn


def default_services() -> ServiceSet:
    """Lazily-importing ServiceSet over the real packages."""
    return ServiceSet(**{f.name: _lazy(f.name) for f in fields(ServiceSet)})


async def prewarm_services(deps: Any, modules: tuple[str, ...] = PREWARM_MODULES) -> dict[str, str]:
    """Call `prewarm(deps)` on every importable package that has one. Never raises."""
    results: dict[str, str] = {}
    for module in modules:
        try:
            # Import off the event loop: heavy ML imports must not stall requests during boot.
            mod = await asyncio.to_thread(importlib.import_module, module)
        except Exception as exc:  # missing package or import-time failure: skip, keep booting
            results[module] = f"unavailable: {type(exc).__name__}"
            log.info("prewarm_skipped", module=module, reason=str(exc)[:200])
            continue
        hook = getattr(mod, "prewarm", None)
        if hook is None:
            results[module] = "no prewarm hook"
            continue
        try:
            await hook(deps)
            results[module] = "ok"
            log.info("prewarm_done", module=module)
        except Exception as exc:
            results[module] = f"failed: {type(exc).__name__}: {exc}"[:200]
            log.warning("prewarm_failed", module=module, error=str(exc)[:300])
    return results
