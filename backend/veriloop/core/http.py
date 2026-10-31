"""Shared httpx.AsyncClient factory and HTTP error classification for tool fns."""

from __future__ import annotations

from typing import Any

import httpx

from veriloop import __version__
from veriloop.core.config import Settings
from veriloop.core.errors import ToolAttemptError
from veriloop.core.logging import redact_text
from veriloop.core.resilience import parse_retry_after

# Retrying these is pointless; everything else in 4xx is also non-retryable except 408/425/429.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def user_agent(settings: Settings) -> str:
    return f"VeriLoop/{__version__} (+{settings.app_base_url}; fact-check research prototype)"


def create_http_client(settings: Settings, *, timeout_s: float = 15.0, **kwargs: Any) -> httpx.AsyncClient:
    """One pooled client per process. Per-call deadlines are enforced by ToolClient, not here."""
    headers = {"User-Agent": user_agent(settings)}
    headers.update(kwargs.pop("headers", {}) or {})
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=5.0),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=30.0),
        follow_redirects=True,
        headers=headers,
        **kwargs,
    )


def error_for_response(response: httpx.Response, *, body_chars: int = 300) -> ToolAttemptError:
    """Map a non-2xx response to a classified ToolAttemptError (retryable or not, Retry-After honoured)."""
    status = response.status_code
    try:
        body = response.text[:body_chars]
    except Exception:  # body may be undecodable/streamed; the status alone is enough
        body = ""
    message = redact_text(f"HTTP {status} {response.reason_phrase}: {body}".strip())
    retry_after = parse_retry_after(response.headers)
    if status == 429:
        return ToolAttemptError(
            "rate_limited", message, retryable=True, retry_after_s=retry_after, status_code=status
        )
    return ToolAttemptError(
        "http_error",
        message,
        retryable=status in _RETRYABLE_STATUS or status >= 500,
        retry_after_s=retry_after,
        status_code=status,
    )


def raise_for_status(response: httpx.Response) -> httpx.Response:
    """Like httpx's raise_for_status, but raises a classified ToolAttemptError."""
    if response.is_success:
        return response
    raise error_for_response(response)
