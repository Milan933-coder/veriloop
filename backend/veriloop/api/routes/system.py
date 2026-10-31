"""GET /health, admin chaos switch (live resilience demo), optional Telegram webhook."""

from __future__ import annotations

import hmac
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from veriloop.bootstrap import build_health
from veriloop.contracts import HealthView
from veriloop.core.chaos import ChaosMode
from veriloop.core.errors import ProblemError
from veriloop.core.tools_registry import TOOL_NAMES

from ..runtime import get_runtime
from ..security import require_admin_key

log = structlog.get_logger(__name__)

router = APIRouter(tags=["system"])


class ChaosRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    mode: ChaosMode


@router.get("/health", response_model=HealthView)
async def health(request: Request) -> JSONResponse:
    """Status, mode, resolved model ids, per-tool status, DB and local-model status, LLM provider."""
    view = await build_health(get_runtime(request).deps)
    return JSONResponse(view.model_dump(mode="json"))


@router.get("/admin/chaos", dependencies=[Depends(require_admin_key)])
async def chaos_state(request: Request) -> JSONResponse:
    return JSONResponse({"active": get_runtime(request).deps.tools.chaos.active()})


@router.post("/admin/chaos", dependencies=[Depends(require_admin_key)])
async def set_chaos(body: ChaosRequest, request: Request) -> JSONResponse:
    """Inject `timeout` / `error` into one registry tool (or `off`): its calls fail fast, labelled chaos."""
    if body.tool not in TOOL_NAMES:
        raise ProblemError(
            422,
            "Unknown tool",
            f"{body.tool!r} is not a registry tool; known: {', '.join(TOOL_NAMES)}.",
            type_="urn:veriloop:problem:validation",
        )
    chaos = get_runtime(request).deps.tools.chaos
    chaos.set(body.tool, body.mode)
    log.warning("chaos_set", tool=body.tool, mode=body.mode, active=chaos.active())
    return JSONResponse({"tool": body.tool, "mode": body.mode, "active": chaos.active()})


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request) -> JSONResponse:
    """Telegram update delivery (TELEGRAM_MODE=webhook only), authenticated by the secret-token header."""
    from veriloop.bots.telegram import SECRET_HEADER

    service: Any = get_runtime(request).telegram
    if service is None or service.mode != "webhook":
        raise ProblemError(404, "Not Found", "The Telegram webhook is not active on this server.")
    supplied = request.headers.get(SECRET_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), service.secret.encode()):
        raise ProblemError(401, "Invalid webhook secret", None, type_="urn:veriloop:problem:unauthorized")
    try:
        update = await request.json()
    except ValueError as exc:
        raise ProblemError(400, "Malformed JSON", "The update is not valid JSON.") from exc
    await service.feed_webhook(update)
    return JSONResponse({"ok": True})
