"""Database access: asyncpg pool with pgvector + JSON codecs, embedded Postgres, schema bootstrap.

When DATABASE_URL is empty we start (or attach to) an embedded Postgres 16 + pgvector via
`pgserver` with data in VERILOOP_HOME/pgdata. It runs as a separate postgres process that outlives
ours (cleanup_mode=None), so restarts and concurrent test runs share it and reconnect instantly.
Production uses the same SQL against pgvector/pgvector:pg16.

Every pooled connection gets: pgvector codec (numpy arrays <-> vector), and json/jsonb codecs
(Python objects <-> jsonb, so repo code passes dicts, never pre-serialised strings).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import structlog
from pgvector.asyncpg import register_vector

from veriloop.core.hashing import normalize_for_json

if TYPE_CHECKING:
    from veriloop.core.config import Settings

log = structlog.get_logger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_SCHEMA_LOCK_KEY = 0x5645524C4F4F50  # "VERLOOP": serialises schema application across processes
_DB_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
EMBEDDED_DB_NAME = "veriloop"

_embedded_servers: dict[str, Any] = {}  # keep pgserver handles referenced for the process lifetime


def _json_dumps(value: Any) -> str:
    return json.dumps(normalize_for_json(value), ensure_ascii=False)


def with_database(dsn: str, dbname: str) -> str:
    """Same server/credentials, different database name."""
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))


def _start_embedded_sync(pgdata: Path) -> str:
    import pgserver  # lazy: only needed without DATABASE_URL

    pgdata.mkdir(parents=True, exist_ok=True)
    key = str(pgdata.resolve())
    server = _embedded_servers.get(key)
    if server is None:
        server = pgserver.get_server(pgdata, cleanup_mode=None)
        _embedded_servers[key] = server
    return str(server.get_uri())


async def embedded_server_dsn(pgdata: Path) -> str:
    """Start or attach to the embedded server (blocking work runs in a thread). Returns the admin DSN."""
    dsn = await asyncio.to_thread(_start_embedded_sync, pgdata)
    log.info("embedded_postgres_ready", pgdata=str(pgdata), dsn=_safe_dsn(dsn))
    return dsn


def _safe_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    user = f"{parts.username}@" if parts.username else ""
    return urlunsplit((parts.scheme, f"{user}{host}{port}", parts.path, "", ""))


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=_json_dumps, decoder=json.loads, schema="pg_catalog")


async def _ensure_vector_extension(dsn: str) -> None:
    # The vector type must exist before register_vector runs in the pool's init hook.
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _SCHEMA_LOCK_KEY)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _SCHEMA_LOCK_KEY)
    finally:
        await conn.close()


def _check_db_name(name: str) -> str:
    if not _DB_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid database name {name!r} (lowercase letters, digits, underscore)")
    return name


async def create_database(admin_dsn: str, name: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{_check_db_name(name)}"')
    finally:
        await conn.close()


async def ensure_database(admin_dsn: str, name: str) -> str:
    """Create `name` if missing (race-safe) and return its DSN."""
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", _check_db_name(name))
        if not exists:
            try:
                await conn.execute(f'CREATE DATABASE "{name}"')
            except asyncpg.DuplicateDatabaseError:
                pass  # another process created it first
    finally:
        await conn.close()
    return with_database(admin_dsn, name)


async def drop_database(admin_dsn: str, name: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_check_db_name(name)}" WITH (FORCE)')
    finally:
        await conn.close()


class Database:
    """Thin wrapper over an asyncpg pool. Repo helpers accept a Database or an asyncpg Connection."""

    def __init__(self, pool: asyncpg.Pool, dsn: str, *, embedded: bool = False) -> None:
        self._pool = pool
        self.dsn = dsn
        self.embedded = embedded

    @classmethod
    async def connect(
        cls,
        settings: Settings | None = None,
        *,
        dsn: str | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
    ) -> Database:
        embedded = False
        if dsn is None:
            if settings is None:
                raise ValueError("Database.connect needs settings or dsn")
            if settings.database_url:
                dsn = settings.database_url
            else:
                # The app gets its own database on the embedded server; tests create vl_test_* ones.
                admin = await embedded_server_dsn(settings.pgdata_dir)
                dsn = await ensure_database(admin, EMBEDDED_DB_NAME)
                embedded = True
        await _ensure_vector_extension(dsn)
        pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size or (settings.db_pool_min if settings else 1),
            max_size=max_size or (settings.db_pool_max if settings else 10),
            init=_init_connection,
            command_timeout=60,
        )
        assert pool is not None
        log.info("db_connected", dsn=_safe_dsn(dsn), embedded=embedded)
        return cls(pool, dsn, embedded=embedded)

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    @property
    def admin_dsn(self) -> str:
        return with_database(self.dsn, "postgres")

    async def apply_schema(self, path: Path = SCHEMA_PATH) -> None:
        sql = await asyncio.to_thread(path.read_text, encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", _SCHEMA_LOCK_KEY)
            try:
                await conn.execute(sql)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", _SCHEMA_LOCK_KEY)
        log.info("db_schema_applied", path=path.name)

    async def close(self) -> None:
        await self._pool.close()

    async def ping(self, timeout_s: float = 2.0) -> bool:
        try:
            return await self._pool.fetchval("SELECT 1", timeout=timeout_s) == 1
        except Exception as exc:
            log.warning("db_ping_failed", error=str(exc))
            return False

    async def server_version(self) -> str:
        return str(await self._pool.fetchval("SHOW server_version"))

    # ---- pass-throughs so repo helpers can take a Database or a Connection --------------------
    async def execute(self, query: str, *args: Any) -> str:
        return await self._pool.execute(query, *args)

    async def executemany(self, query: str, args: Any) -> None:
        await self._pool.executemany(query, args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        return await self._pool.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        return await self._pool.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._pool.fetchval(query, *args)

    def acquire(self) -> Any:
        return self._pool.acquire()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """`async with db.transaction() as conn:` then pass `conn` to repo helpers."""
        async with self._pool.acquire() as conn, conn.transaction():
            yield conn

    # ---- test helpers ------------------------------------------------------------------------
    async def create_database(self, name: str) -> str:
        await create_database(self.admin_dsn, name)
        return with_database(self.dsn, name)

    async def drop_database(self, name: str) -> None:
        await drop_database(self.admin_dsn, name)
