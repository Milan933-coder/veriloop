"""Live demo: tamper with one audit_chain row so GET /audit/{job_id}/verify returns ok:false, broken_at:N.

    cd backend
    python scripts/tamper_demo.py                 # latest VERDICT row: rewrite its payload (hash unchanged)
    python scripts/tamper_demo.py --job <job_id>  # that job's VERDICT row
    python scripts/tamper_demo.py --row 42 --rehash   # also recompute payload_hash -> signature fails
    python scripts/restore_chain.py               # put the original row back (ok:true again)

How it works: connects as the database superuser (the embedded pgserver user), saves the original
row to VERILOOP_HOME/audit_tamper_backup.json, and inside ONE transaction disables the append-only
trigger, flips the verdict in payload_json, and re-enables the trigger (other sessions never see the
trigger disabled). Refuses to run twice without a restore in between.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from veriloop.core.config import Settings, get_settings
from veriloop.core.hashing import canonical_json, sha256_hex
from veriloop.db.engine import Database

TRIGGER = "audit_chain_no_update_delete"
FLIP = {
    "FALSE": "TRUE",
    "TRUE": "FALSE",
    "MANIPULATED": "TRUE",
    "UNVERIFIABLE": "TRUE",
    "NOT_CHECKABLE": "TRUE",
}


def default_backup_path(settings: Settings) -> Path:
    return settings.home / "audit_tamper_backup.json"


async def find_row(db: Any, *, row_id: int | None = None, job_id: str | None = None) -> Any:
    if row_id is not None:
        return await db.fetchrow(
            "SELECT id, job_id, payload_json, payload_hash FROM audit_chain WHERE id = $1", row_id
        )
    if job_id is not None:
        return await db.fetchrow(
            "SELECT id, job_id, payload_json, payload_hash FROM audit_chain "
            "WHERE job_id = $1 AND event_type = 'verdict' "
            "ORDER BY id DESC LIMIT 1",
            job_id,
        )
    return await db.fetchrow(
        "SELECT id, job_id, payload_json, payload_hash FROM audit_chain WHERE event_type = 'verdict' "
        "ORDER BY id DESC LIMIT 1"
    )


def forge_payload(payload_json: str) -> tuple[str, str]:
    """A plausible forgery: flip the verdict and claim 99 % confidence. Returns (new json, what changed)."""
    payload = json.loads(payload_json)
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else None
    if verdict is not None:
        old = verdict.get("label")
        verdict["label"] = FLIP.get(str(old), "TRUE")
        verdict["confidence"] = 0.99
        change = f"verdict.label {old} -> {verdict['label']}, confidence -> 0.99"
    else:
        payload["tampered"] = True
        change = "added field tampered=true"
    return canonical_json(payload), change


async def set_row(db: Database, row_id: int, payload_json: str, payload_hash: str) -> None:
    async with db.transaction() as conn:
        await conn.execute(f"ALTER TABLE audit_chain DISABLE TRIGGER {TRIGGER}")
        await conn.execute(
            "UPDATE audit_chain SET payload_json = $2, payload_hash = $3 WHERE id = $1",
            row_id,
            payload_json,
            payload_hash,
        )
        await conn.execute(f"ALTER TABLE audit_chain ENABLE TRIGGER {TRIGGER}")


async def tamper(
    db: Database,
    backup_path: Path,
    *,
    row_id: int | None = None,
    job_id: str | None = None,
    rehash: bool = False,
) -> dict[str, Any]:
    if backup_path.exists():  # noqa: ASYNC240 - one tiny local file in a CLI script
        raise RuntimeError(f"{backup_path} exists: the chain is already tampered; run restore_chain.py first")
    row = await find_row(db, row_id=row_id, job_id=job_id)
    if row is None:
        raise RuntimeError("no matching audit_chain row (run a verification first)")
    forged, change = forge_payload(row["payload_json"])
    new_hash = sha256_hex(forged) if rehash else row["payload_hash"]
    backup = {
        "id": row["id"],
        "job_id": row["job_id"],
        "payload_json": row["payload_json"],
        "payload_hash": row["payload_hash"],
        "database": db.dsn.rsplit("/", 1)[-1],
    }
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(json.dumps(backup, ensure_ascii=False), encoding="utf-8")  # noqa: ASYNC240
    await set_row(db, row["id"], forged, new_hash)
    return {
        "row_id": row["id"],
        "job_id": row["job_id"],
        "change": change,
        "rehash": rehash,
        "backup": str(backup_path),
    }


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--job", help="tamper with this job's VERDICT row")
    target.add_argument("--row", type=int, help="tamper with this audit_chain id")
    parser.add_argument(
        "--rehash", action="store_true", help="also recompute payload_hash (signature then fails)"
    )
    parser.add_argument(
        "--backup", type=Path, help="backup file (default VERILOOP_HOME/audit_tamper_backup.json)"
    )
    args = parser.parse_args(argv)

    from veriloop.services.audit import verify_chain

    settings = get_settings()
    db = await Database.connect(settings, min_size=1, max_size=2)
    try:
        info = await tamper(
            db,
            args.backup or default_backup_path(settings),
            row_id=args.row,
            job_id=args.job,
            rehash=args.rehash,
        )
        result = await verify_chain(SimpleNamespace(db=db, settings=settings), info["job_id"])  # type: ignore[arg-type]
    finally:
        await db.close()
    print(json.dumps({"tampered": info, "verify": result.model_dump(mode="json")}, indent=2))
    print(
        f"\nNow open GET /audit/{info['job_id']}/verify -> ok:false broken_at:{info['row_id']}. "
        "Restore with: python scripts/restore_chain.py",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
