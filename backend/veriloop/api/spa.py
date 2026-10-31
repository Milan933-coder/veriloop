"""Serve the built web app (frontend/dist) from `/` with an index.html fallback for client routes.

Registered last, so every API route takes precedence. Files that exist in dist are served directly
(hashed /assets/* cached for a year; index.html, sw.js, registerSW.js and the manifest with
`Cache-Control: no-cache` so a new deploy is picked up). Any other GET that is not an API path and
does not look like a file gets index.html (client routes /, /j/{id}, /a/{id}, /share, /about).
If dist was never built, `/` answers with a small JSON hint instead.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from veriloop import __version__
from veriloop.core.config import REPO_ROOT
from veriloop.core.errors import ProblemError

DEFAULT_DIST = REPO_ROOT / "frontend" / "dist"
API_PREFIXES = frozenset(
    {"verify", "report", "legal", "audit", "health", "admin", "telegram", "docs", "openapi.json", "redoc"}
)
NO_CACHE = frozenset({"index.html", "sw.js", "registerSW.js", "manifest.webmanifest"})
# Explicit types: on Windows `mimetypes` reads the registry, which can map .js to text/plain and
# break module scripts under strict MIME checking.
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".txt": "text/plain; charset=utf-8",
}


def cache_control(rel_path: str) -> str:
    if rel_path in NO_CACHE:
        return "no-cache"
    if rel_path.startswith("assets/"):
        return "public, max-age=31536000, immutable"  # content-hashed file names
    return "public, max-age=3600"


def safe_file(dist: Path, rel_path: str) -> Path | None:
    """The file under dist for a URL path, or None (also None for anything escaping dist)."""
    if not rel_path or "\\" in rel_path or "\x00" in rel_path:
        return None
    root = dist.resolve()
    try:
        candidate = (root / rel_path).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


def _file(path: Path, rel_path: str) -> FileResponse:
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={"Cache-Control": cache_control(rel_path)},
    )


def mount_spa(app: FastAPI, dist: Path | None) -> None:
    index = dist / "index.html" if dist is not None else None
    built = index is not None and index.is_file()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str, request: Request) -> Response:
        first = full_path.split("/", 1)[0]
        if first in API_PREFIXES:
            raise ProblemError(404, "Not Found", f"No API route for {request.url.path}.")
        if not built:
            if full_path == "":
                return JSONResponse(
                    {
                        "service": "VeriLoop API",
                        "version": __version__,
                        "hint": "The web app is not built: run `npm run build` in frontend/ "
                        "(or use the Vite dev server on :5173). API docs: /docs, health: /health.",
                    }
                )
            raise ProblemError(404, "Not Found", "The web app is not built on this server.")
        assert dist is not None and index is not None
        found = safe_file(dist, full_path)
        if found is not None:
            return _file(found, full_path)
        last = full_path.rsplit("/", 1)[-1]
        if first in ("assets", "icons") or "." in last:
            raise ProblemError(404, "Not Found", f"No such file: /{full_path}.")
        return _file(index, "index.html")
