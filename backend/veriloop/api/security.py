"""Auth: `X-API-Key` when API_KEYS is set; `X-Admin-Key` for admin endpoints.

The web app / PWA is served from this same origin and never embeds a key (EventSource cannot send
headers anyway), so same-origin browser requests are allowed without one, still rate limited
(BUILD_SPEC §9). "Same origin" = the browser's `Sec-Fetch-Site: same-origin` (a header page scripts
cannot set) or an `Origin` equal to this server / APP_BASE_URL. A non-browser client can forge both,
so this is a convenience gate for the demo deployment, not a security boundary; the key is.
Public by design: GET /health, GET /audit/* (anyone must be able to verify a record, e.g. from a QR
code), the SPA, and /telegram/webhook (checked against its own secret).
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import Request

from veriloop.core.errors import ProblemError

from .runtime import get_runtime


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def is_same_origin(request: Request, app_base_url: str) -> bool:
    if request.headers.get("sec-fetch-site", "").lower() == "same-origin":
        return True
    origin = request.headers.get("origin", "").rstrip("/").lower()
    if not origin or origin == "null":
        return False
    return origin in {_origin(str(request.base_url)), _origin(app_base_url)}


def key_matches(supplied: str | None, keys: list[str]) -> bool:
    if not supplied:
        return False
    # compare_digest on every key: timing does not reveal which (or whether a prefix) matched
    return any(hmac.compare_digest(supplied.encode(), key.encode()) for key in keys)


async def require_api_key(request: Request) -> None:
    """Dependency for the job/report/legal endpoints."""
    settings = get_runtime(request).deps.settings
    keys = settings.api_key_list
    if not keys:
        return
    if key_matches(request.headers.get("x-api-key"), keys) or is_same_origin(request, settings.app_base_url):
        return
    raise ProblemError(
        401,
        "API key required",
        "Send a valid X-API-Key header (or use the web app served from this server).",
        type_="urn:veriloop:problem:unauthorized",
    )


async def require_admin_key(request: Request) -> None:
    settings = get_runtime(request).deps.settings
    admin = settings.admin_key.get_secret_value()
    if not admin:
        raise ProblemError(
            403, "Admin endpoints disabled", "ADMIN_KEY is empty.", type_="urn:veriloop:problem:forbidden"
        )
    if not key_matches(request.headers.get("x-admin-key"), [admin]):
        raise ProblemError(
            401,
            "Admin key required",
            "Send the X-Admin-Key header.",
            type_="urn:veriloop:problem:unauthorized",
        )
