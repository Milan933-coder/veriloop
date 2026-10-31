"""Undo scripts/tamper_demo.py: write the saved original audit_chain row back, then verify the chain.

cd backend
python scripts/restore_chain.py            # uses VERILOOP_HOME/audit_tamper_backup.json
python scripts/restore_chain.py --backup other.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from veriloop.core.config import get_settings
from veriloop.db.engine import Database

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tamper_demo import default_backup_path, set_row


async def restore(db: Database, backup_path: Path) -> dict[str, Any]:
    if not backup_path.exists():  # noqa: ASYNC240 - one tiny local file in a CLI script
        raise RuntimeError(f"no backup at {backup_path}: nothing to restore")
    backup = json.loads(backup_path.read_text(encoding="utf-8"))  # noqa: ASYNC240
    await set_row(db, int(backup["id"]), backup["payload_json"], backup["payload_hash"])
    backup_path.unlink()  # noqa: ASYNC240
    return {"row_id": backup["id"], "job_id": backup["job_id"]}


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore the audit row changed by tamper_demo.py")
    parser.add_argument(
        "--backup", type=Path, help="backup file (default VERILOOP_HOME/audit_tamper_backup.json)"
    )
    args = parser.parse_args(argv)

    from veriloop.services.audit import verify_chain

    settings = get_settings()
    db = await Database.connect(settings, min_size=1, max_size=2)
    try:
        info = await restore(db, args.backup or default_backup_path(settings))
        result = await verify_chain(SimpleNamespace(db=db, settings=settings), info["job_id"])  # type: ignore[arg-type]
    finally:
        await db.close()
    print(json.dumps({"restored": info, "verify": result.model_dump(mode="json")}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
