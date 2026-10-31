"""Exception types shared by every package."""

from __future__ import annotations

from typing import Any

from veriloop.contracts import ToolError


class VeriLoopError(Exception):
    """Base class for VeriLoop errors."""


class ToolCallFailed(VeriLoopError):
    """A tool call failed after retries (or was refused/unconfigured). Carries the logged ToolError (G3)."""

    def __init__(self, error: ToolError) -> None:
        self.error = error
        super().__init__(f"{error.tool}:{error.op} failed ({error.error_type}): {error.message}")


class ToolAttemptError(VeriLoopError):
    """Raised *inside* a tool fn to classify one failed attempt for ToolClient.

    Tool fns may also just let httpx exceptions propagate; ToolClient classifies those itself.
    `retryable` failures (timeouts, 5xx, 429, transport errors) are retried and count toward the
    circuit breaker; non-retryable ones (4xx, parse errors) fail immediately.
    """

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: float | None = None,
        status_code: int | None = None,
    ) -> None:
        self.error_type = error_type
        self.message = message
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.status_code = status_code
        super().__init__(f"{error_type}: {message}")


class LLMDisabled(VeriLoopError):
    """LLM_PROVIDER=none: callers must use their extractive/heuristic path (D-01b)."""


class ModelResolutionError(VeriLoopError):
    """Boot-time LLM model resolution failed; the process must exit non-zero (D-01)."""


class ModelNotAllowed(VeriLoopError):
    """Attempt to call a model id that was not resolved from LLM_ALLOWED_MODELS."""


class MLUnavailable(VeriLoopError):
    """A local model is disabled (ML_ENABLE=false) or failed to import/download/load."""


class ProblemError(VeriLoopError):
    """An HTTP error rendered as RFC 7807 `application/problem+json` by the API layer."""

    media_type = "application/problem+json"

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        type_: str = "about:blank",
        instance: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type_
        self.instance = instance
        self.extra = extra or {}
        super().__init__(f"{status} {title}" + (f": {detail}" if detail else ""))

    def to_problem(self, *, request_id: str | None = None, instance: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "instance": instance or self.instance,
            "request_id": request_id,
        }
        body.update(self.extra)
        return body
