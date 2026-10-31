"""RFC 7807 `application/problem+json` for every error (SPEC §10, BUILD_SPEC §9).

Body: {type, title, status, detail, instance, request_id} (+ optional extension members such as
`errors` for validation failures). The request-context middleware adds `latency_ms` like it does
to every JSON body.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from veriloop.core.errors import ProblemError

log = structlog.get_logger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def problem_body(
    status: int,
    title: str,
    detail: str | None = None,
    *,
    type_: str = "about:blank",
    instance: str | None = None,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": type_,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": request_id,
    }
    body.update(extra or {})
    return body


def problem_response(
    request: Request, exc: ProblemError, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = exc.to_problem(request_id=request_id_of(request), instance=exc.instance or request.url.path)
    return JSONResponse(body, status_code=exc.status, media_type=PROBLEM_MEDIA_TYPE, headers=headers)


def validation_problem(errors: list[Any], detail_prefix: str = "Invalid request") -> ProblemError:
    """422 from pydantic/FastAPI errors with a compact, JSON-safe `errors` list."""
    clean = [
        {
            "loc": [str(p) for p in e.get("loc", ())],
            "msg": str(e.get("msg", "")),
            "type": str(e.get("type", "")),
        }
        for e in errors
    ]
    summary = "; ".join(f"{'.'.join(e['loc'][-2:]) or 'body'}: {e['msg']}" for e in clean[:5])
    return ProblemError(
        422,
        "Invalid request",
        f"{detail_prefix}: {summary}" if summary else detail_prefix,
        type_="urn:veriloop:problem:validation",
        extra={"errors": clean},
    )


async def _problem_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return problem_response(request, exc)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    title = phrase(exc.status_code)
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
    return problem_response(request, ProblemError(exc.status_code, title, detail), headers=exc.headers)


async def _request_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError | ValidationError)
    return problem_response(request, validation_problem(list(exc.errors())))


async def _rate_limited(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RateLimitExceeded)
    log.info("rate_limited", path=request.url.path, limit=str(exc.detail))
    problem = ProblemError(
        429,
        "Too many requests",
        f"Rate limit exceeded ({exc.detail}). Please wait a minute before submitting again.",
        type_="urn:veriloop:problem:rate-limited",
    )
    return problem_response(request, problem, headers={"Retry-After": "60"})


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(RateLimitExceeded, _rate_limited)  # before the generic HTTPException
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(ValidationError, _request_validation)
