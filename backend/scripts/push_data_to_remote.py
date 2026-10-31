"""Copy the locally built reference data (ClaimReview index + known-fake index) into another Postgres.

Rebuilding these on a host such as Railway means re-parsing the ~200 MB feed and re-embedding ~42k
reviews on that host's CPU. Copying the finished rows (with their vectors) takes a few minutes instead.

    cd backend
    python scripts/push_data_to_remote.py --target "postgresql://postgres:<pw>@<host>.proxy.rlwy.net:<port>/railway"
    python scripts/push_data_to_remote.py --target ... --truncate        # replace rows already there
    python scripts/push_data_to_remote.py --target ... --apply-schema    # target has never booted the app

The source is this machine's database (DATABASE_URL, or the embedded server under VERILOOP_HOME).
The target needs the schema: boot the deployed app once (it applies schema.sql on startup) or pass
--apply-schema. Uses the pg_dump / pg_restore binaries bundled with pgserver, so nothing else to install.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import asyncpg

from veriloop.core.config import get_settings
from veriloop.db.engine import (
    EMBEDDED_DB_NAME,
    Database,
    _safe_dsn,
    embedded_server_dsn,
    ensure_database,
)

DEFAULT_TABLES = ("claimreview_index", "known_fakes")


def pg_bin(name: str) -> str:
    """pg_dump/pg_restore from the pgserver wheel, else from PATH."""
    try:
        import pgserver

        exe = Path(pgserver.__file__).parent / "pginstall" / "bin" / (name + (".exe" if sys.platform == "win32" else ""))
        if exe.exists():
            return str(exe)
    except ImportError:
        pass
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} not found (install PostgreSQL client tools or pgserver)")
    return found


async def source_dsn() -> str:
    settings = get_settings()
    if settings.database_url:
        return settings.database_url
    admin = await embedded_server_dsn(settings.pgdata_dir)
    return await ensure_database(admin, EMBEDDED_DB_NAME)


async def row_counts(dsn: str, tables: list[str]) -> dict[str, int]:
    conn = await asyncpg.connect(dsn)
    try:
        return {t: await conn.fetchval(f'SELECT count(*) FROM "{t}"') for t in tables}
    finally:
        await conn.close()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, help="target Postgres URL (use the public/TCP-proxy URL)")
    ap.add_argument("--tables", default=",".join(DEFAULT_TABLES), help="comma-separated table names")
    ap.add_argument("--truncate", action="store_true", help="empty the target tables first")
    ap.add_argument("--apply-schema", action="store_true", help="apply schema.sql to the target first")
    args = ap.parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    unknown = set(tables) - set(DEFAULT_TABLES)
    if unknown:
        # Only reference data: jobs and the audit chain belong to the deployment that produced them.
        raise SystemExit(f"refusing to copy {sorted(unknown)}; allowed: {', '.join(DEFAULT_TABLES)}")

    src = await source_dsn()
    print(f"source: {_safe_dsn(src)}\ntarget: {_safe_dsn(args.target)}")
    print("source rows:", await row_counts(src, tables))

    if args.apply_schema:
        db = await Database.connect(dsn=args.target, min_size=1, max_size=1)
        try:
            await db.apply_schema()
        finally:
            await db.close()
        print("target schema applied")
    if args.truncate:
        conn = await asyncpg.connect(args.target)
        try:
            await conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables))
        finally:
            await conn.close()
        print("target tables truncated")

    settings = get_settings()
    dump = settings.home / "tmp" / "reference_data.dump"
    dump.parent.mkdir(parents=True, exist_ok=True)
    table_args = [a for t in tables for a in ("--table", t)]
    subprocess.run(
        [pg_bin("pg_dump"), "--data-only", "--format=custom", "--no-owner", "--no-privileges",
         *table_args, "--file", str(dump), "--dbname", src],
        check=True,
    )
    print(f"dumped {dump.stat().st_size / 1e6:.1f} MB")
    restore = subprocess.run(
        [pg_bin("pg_restore"), "--data-only", "--no-owner", "--no-privileges", "--exit-on-error",
         "--dbname", args.target, str(dump)],
    )
    after = await row_counts(args.target, tables)
    print("target rows:", after)
    dump.unlink(missing_ok=True)
    if restore.returncode != 0:
        print("pg_restore failed (rows already present? rerun with --truncate)", file=sys.stderr)
        return restore.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
