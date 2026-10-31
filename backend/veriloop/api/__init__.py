"""HTTP API (BUILD_SPEC §9): app factory, middleware, problem+json, routes, SPA serving."""

from .app import create_app

__all__ = ["create_app"]
