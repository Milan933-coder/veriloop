"""Postgres (+pgvector) access: schema, connection pool / embedded server, typed repository helpers."""

from .engine import Database

__all__ = ["Database"]
