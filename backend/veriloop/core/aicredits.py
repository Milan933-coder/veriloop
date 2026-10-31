"""Raw HTTP transport for aicredits.in, the OpenAI-compatible router mandated by D-01.

Only shapes requests and maps failures to classified ToolAttemptErrors; resilience, caching and
cassettes are ToolClient's job. Facts verified 2026-10-31:
* `GET /models` does not check the key (an invalid key still gets 200), hence the boot probe call.
* 401 `{"error":{"code":401,"message":"Invalid API Key","type":"authentication_error"}}` is fatal.
* Upstream 4xx are wrapped: `{"error":{"message":"Provider returned error","code":400,
  "metadata":{"raw":"<upstream JSON string>"}}}` -> unwrap `metadata.raw` for a useful message.
* 429 carries `X-Ratelimit-Reset` (seconds) -> retryable with that wait.
* The API key travels only in the Authorization header; it is never part of a returned payload.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from veriloop.core.config import Settings
from veriloop.core.errors import ToolAttemptError
from veriloop.core.logging import redact_text
from veriloop.core.resilience import parse_retry_after

# Response headers worth keeping (logged per call, stored in cassettes). Lower-case.
KEEP_HEADERS = (
    "x-request-id",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-aicredits-cost-inr",
)


def _unwrap_upstream(raw: Any) -> str:
    """`metadata.raw` is usually a JSON string from the upstream provider; extract its message/code."""
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw[:500]
    if isinstance(raw, dict):
        err = raw.get("error", raw)
        if isinstance(err, dict):
            parts = [str(err.get(k)) for k in ("code", "type", "param", "message") if err.get(k)]
            return " ".join(parts)[:500]
    return str(raw)[:500]


def api_error(status: int, body: Any, headers: httpx.Headers | dict[str, str]) -> ToolAttemptError:
    """Classify an aicredits error response (non-2xx, or a 2xx body carrying `error`)."""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message") or "")
        err_type = str(err.get("type") or "")
        upstream = _unwrap_upstream((err.get("metadata") or {}).get("raw"))
        if isinstance(err.get("code"), int) and status < 400:
            status = int(err["code"])
    else:
        message, err_type, upstream = (str(body)[:300] if body else ""), "", ""
    request_id = headers.get("x-request-id") or headers.get("X-Request-Id")
    text = f"HTTP {status}" + (f" {err_type}" if err_type else "") + f": {message}"
    if upstream:
        text += f" | upstream: {upstream}"
    if request_id:
        text += f" (request_id={request_id})"
    text = redact_text(text)
    retry_after = parse_retry_after(dict(headers))
    if status == 429:
        return ToolAttemptError(
            "rate_limited", text, retryable=True, retry_after_s=retry_after, status_code=status
        )
    if status in (401, 403):
        # Bad/revoked key or forbidden model: retrying cannot help and burns the boot deadline.
        return ToolAttemptError("http_error", text, retryable=False, status_code=status)
    retryable = status >= 500 or status in (408, 425)
    return ToolAttemptError(
        "http_error", text, retryable=retryable, retry_after_s=retry_after, status_code=status
    )


class AicreditsAPI:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self._key = settings.llm_api_key
        self._http = http

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key.get_secret_value()}", "Accept": "application/json"}

    async def _send(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        response = await self._http.request(
            method, f"{self.base_url}{path}", json=payload, headers=self._headers(), timeout=None
        )
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text[:300]
        kept = {h: response.headers[h] for h in KEEP_HEADERS if h in response.headers}
        if not response.is_success or (
            isinstance(body, dict) and "error" in body and "choices" not in body and "data" not in body
        ):
            raise api_error(response.status_code, body, response.headers)
        if not isinstance(body, dict):
            raise ToolAttemptError("parse_error", f"non-JSON response from {path}: {str(body)[:200]}")
        return body, kept

    async def list_models(self) -> dict[str, Any]:
        """Model listing trimmed to the fields resolution needs (keeps cassettes small)."""
        body, _ = await self._send("GET", "/models")
        data = body.get("data")
        if not isinstance(data, list):
            raise ToolAttemptError("parse_error", "GET /models: response has no `data` list")
        keep = ("id", "is_active", "supported_apis", "name")
        return {
            "data": [{k: m[k] for k in keep if k in m} for m in data if isinstance(m, dict) and "id" in m]
        }

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /chat/completions. Returns `{"body": <response JSON>, "headers": {<kept headers>}}`."""
        body, kept = await self._send("POST", "/chat/completions", payload)
        return {"body": body, "headers": kept}
