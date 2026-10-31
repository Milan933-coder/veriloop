"""Request context middleware (pure ASGI, so SSE streams pass through untouched).

* `X-Request-Id`: taken from the request when it is a sane token, otherwise generated; echoed on every
  response and bound to structlog's context for every log line of the request.
* `latency_ms`: added to the body of every JSON / problem+json response (and `X-Latency-Ms` to every
  response). JSON bodies are buffered (they are small) and rewritten with the two members.
* Error boundary: an unhandled exception becomes a 500 problem+json (logged with traceback) instead
  of a bare text response, so every error the client sees has the RFC 7807 shape.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .problems import PROBLEM_MEDIA_TYPE, problem_body

log = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"
LATENCY_HEADER = "X-Latency-Ms"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_JSON_TYPES = ("application/json", PROBLEM_MEDIA_TYPE)
# Polled constantly by the SPA; logging every hit would drown the useful lines.
_QUIET_PREFIXES = ("/assets/", "/icons/", "/health")
# Machine-readable documents whose schema forbids extra top-level members (OpenAPI 3.1).
_NO_INJECT = frozenset({"/openapi.json"})


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"x-request-id":
            text = value.decode("latin-1").strip()
            return text if _SAFE_ID.fullmatch(text) else None
    return None


def inject_meta(body: bytes, request_id: str, latency_ms: int) -> bytes:
    """Add request_id + latency_ms to a JSON object body (other JSON values pass unchanged)."""
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return body
    if not isinstance(data, dict):
        return body
    data["request_id"] = request_id
    data["latency_ms"] = latency_ms
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        t0 = time.perf_counter()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["t0"] = t0
        path: str = scope.get("path", "")
        held: dict[str, Any] = {}
        chunks: list[bytes] = []
        started = False
        status_code = 0

        def latency() -> int:
            return int((time.perf_counter() - t0) * 1000)

        async def send_wrapper(message: Message) -> None:
            nonlocal started, status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(raw=list(message.get("headers", [])))
                if headers.get("content-type", "").startswith(_JSON_TYPES) and path not in _NO_INJECT:
                    held["start"] = {**message, "headers": headers.raw}  # wait for the body
                    return
                headers[REQUEST_ID_HEADER] = request_id
                headers[LATENCY_HEADER] = str(latency())
                started = True
                await send({**message, "headers": headers.raw})
                return
            if message["type"] == "http.response.body" and "start" in held:
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                ms = latency()
                body = inject_meta(b"".join(chunks), request_id, ms)
                headers = MutableHeaders(raw=held.pop("start")["headers"])
                headers["content-length"] = str(len(body))
                headers[REQUEST_ID_HEADER] = request_id
                headers[LATENCY_HEADER] = str(ms)
                started = True
                await send({"type": "http.response.start", "status": status_code, "headers": headers.raw})
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                log.exception("unhandled_error", method=scope.get("method"), path=path)
                if started:
                    raise  # headers already sent: nothing better to do than drop the connection
                status_code = 500
                body = problem_body(
                    500,
                    "Internal Server Error",
                    f"Unexpected {type(exc).__name__}; the error was logged with this request_id.",
                    instance=path,
                    request_id=request_id,
                    extra={"latency_ms": latency()},
                )
                payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", PROBLEM_MEDIA_TYPE.encode()),
                            (b"content-length", str(len(payload)).encode()),
                            (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
                            (LATENCY_HEADER.lower().encode(), str(latency()).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload, "more_body": False})
            finally:
                if not path.startswith(_QUIET_PREFIXES):
                    log.info(
                        "http_request",
                        method=scope.get("method"),
                        path=path,
                        status=status_code,
                        latency_ms=latency(),
                    )
