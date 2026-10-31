"""Hash-chained, Ed25519-signed audit log (BUILD_SPEC §8).

Row invariants (checked by `verify_chain`, recomputed from genesis on every call):
  * payload_json is canonical JSON (core.hashing.canonical_json: sorted keys, (",", ":") separators,
    ensure_ascii=False, UTC ISO datetimes, floats rounded to 6 dp) and is stored as exact text;
  * payload_hash = sha256(payload_json) (lowercase hex);
  * prev_hash = payload_hash of the previous row by id (genesis = 64 zeros);
  * signature = base64 Ed25519 over f"{prev_hash}:{payload_hash}", by the key named in key_fingerprint;
  * the job_id / event_type / created_at / resolved_model_ids / tool_versions columns equal the
    same fields inside the signed payload, so editing a column alone is detected too.

Appends serialise on a transaction-scoped advisory lock, so concurrent writers (5 jobs finishing at
once, report confirmations) can never fork the chain; the lock is held only for read-prev + insert.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

import asyncpg
import structlog

from veriloop.contracts import AuditEntryView, AuditEventType, AuditVerifyResult, utcnow
from veriloop.core.config import Settings
from veriloop.core.hashing import canonical_json, normalize_for_json, sha256_hex
from veriloop.db.engine import Database

from .keys import load_signing_key

log = structlog.get_logger(__name__)

GENESIS_HASH = "0" * 64
PAYLOAD_SCHEMA = "veriloop.audit/v1"
AUDIT_LOCK_KEY = 0x5641554449545F43  # "VAUDIT_C": serialises appends across processes
# Fields append_event always sets itself (the caller cannot spoof them).
RESERVED_FIELDS = ("schema", "job_id", "event_type", "recorded_at", "resolved_model_ids", "tool_versions")

_COLS = (
    "id, job_id, event_type, created_at, prev_hash, payload_hash, payload_json, resolved_model_ids, "
    "tool_versions, signature, key_fingerprint"
)


class AuditReadDeps(Protocol):
    settings: Settings
    db: Database


class _Tools(Protocol):
    def tool_versions(self) -> dict[str, str]: ...


class AuditWriteDeps(AuditReadDeps, Protocol):
    tools: _Tools
    resolved_models: dict[str, str]


def _entry(row: Any) -> AuditEntryView:
    return AuditEntryView(
        id=row["id"],
        job_id=row["job_id"],
        event_type=AuditEventType(row["event_type"]),
        created_at=row["created_at"],
        prev_hash=row["prev_hash"],
        payload_hash=row["payload_hash"],
        payload_json=row["payload_json"],
        resolved_model_ids=dict(row["resolved_model_ids"] or {}),
        tool_versions=dict(row["tool_versions"] or {}),
        signature=row["signature"],
        key_fingerprint=row["key_fingerprint"],
    )


def build_payload(
    job_id: str,
    event_type: AuditEventType,
    payload: dict[str, Any],
    *,
    recorded_at: datetime,
    resolved_model_ids: dict[str, str],
    tool_versions: dict[str, str],
) -> tuple[str, str]:
    """(canonical payload_json, payload_hash) for one row. Reserved fields override the caller's."""
    body = {
        **payload,
        "schema": PAYLOAD_SCHEMA,
        "job_id": job_id,
        "event_type": AuditEventType(event_type).value,
        "recorded_at": recorded_at,
        "resolved_model_ids": resolved_model_ids,
        "tool_versions": tool_versions,
    }
    payload_json = canonical_json(body)
    return payload_json, sha256_hex(payload_json)


async def append_event(
    deps: AuditWriteDeps, job_id: str, event_type: AuditEventType, payload: dict[str, Any]
) -> AuditEntryView:
    """Append one signed row. Raises on DB failure (callers decide how to degrade)."""
    key = load_signing_key(deps.settings)
    # G7: resolved model ids + tool versions go into every row (columns and signed payload).
    resolved = {str(k): str(v) for k, v in dict(deps.resolved_models).items()}
    versions = {str(k): str(v) for k, v in deps.tools.tool_versions().items()}
    recorded_at = utcnow()
    payload_json, payload_hash = build_payload(
        job_id,
        event_type,
        payload,
        recorded_at=recorded_at,
        resolved_model_ids=resolved,
        tool_versions=versions,
    )
    async with deps.db.transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", AUDIT_LOCK_KEY)
        prev = await conn.fetchval("SELECT payload_hash FROM audit_chain ORDER BY id DESC LIMIT 1")
        prev_hash = prev or GENESIS_HASH
        signature = key.sign(f"{prev_hash}:{payload_hash}")
        row = await conn.fetchrow(
            f"""
            INSERT INTO audit_chain (job_id, event_type, created_at, prev_hash, payload_hash, payload_json,
                                     resolved_model_ids, tool_versions, signature, key_fingerprint)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING {_COLS}
            """,
            job_id,
            AuditEventType(event_type).value,
            recorded_at,
            prev_hash,
            payload_hash,
            payload_json,
            resolved,
            versions,
            signature,
            key.fingerprint,
        )
    entry = _entry(row)
    log.info(
        "audit_appended",
        job_id=job_id,
        event_type=entry.event_type.value,
        audit_id=entry.id,
        payload_hash=payload_hash,
        prev_hash=prev_hash,
    )
    return entry


async def get_entry(
    deps: AuditReadDeps, job_id: str, event_type: AuditEventType = AuditEventType.VERDICT
) -> AuditEntryView | None:
    """Latest row of `event_type` for the job (None when nothing was written yet)."""
    row = await deps.db.fetchrow(
        f"SELECT {_COLS} FROM audit_chain WHERE job_id = $1 AND event_type = $2 ORDER BY id DESC LIMIT 1",
        job_id,
        AuditEventType(event_type).value,
    )
    return _entry(row) if row else None


async def get_entries(deps: AuditReadDeps, job_id: str) -> list[AuditEntryView]:
    rows = await deps.db.fetch(f"SELECT {_COLS} FROM audit_chain WHERE job_id = $1 ORDER BY id", job_id)
    return [_entry(r) for r in rows]


async def get_entry_by_id(deps: AuditReadDeps, audit_id: int) -> AuditEntryView | None:
    row = await deps.db.fetchrow(f"SELECT {_COLS} FROM audit_chain WHERE id = $1", audit_id)
    return _entry(row) if row else None


def check_row(row: Any, expected_prev: str, key_fingerprint: str, verify_sig: Any) -> str | None:
    """Return why this row breaks the chain, or None if it is intact."""
    payload_json: str = row["payload_json"]
    payload_hash: str = row["payload_hash"]
    if row["prev_hash"] != expected_prev:
        return (
            f"prev_hash {row['prev_hash'][:12]}... does not equal the previous row's payload_hash "
            f"{expected_prev[:12]}... (a row was modified, inserted or deleted)"
        )
    if sha256_hex(payload_json) != payload_hash:
        return "payload_hash does not match sha256(payload_json): the payload was modified"
    if row["key_fingerprint"] != key_fingerprint:
        return f"row is signed by an unknown key (fingerprint {row['key_fingerprint'][:16]}...)"
    if not verify_sig(f"{row['prev_hash']}:{payload_hash}", row["signature"]):
        return "invalid Ed25519 signature over prev_hash:payload_hash"
    try:
        payload = json.loads(payload_json)
    except ValueError:
        return "payload_json is not valid JSON"
    if not isinstance(payload, dict):
        return "payload_json is not a JSON object"
    if canonical_json(payload) != payload_json:
        return "payload_json is not in canonical form"
    expected = {
        "job_id": row["job_id"],
        "event_type": row["event_type"],
        "recorded_at": normalize_for_json(row["created_at"]),
        "resolved_model_ids": dict(row["resolved_model_ids"] or {}),
        "tool_versions": dict(row["tool_versions"] or {}),
    }
    for name, value in expected.items():
        if payload.get(name) != value:
            return f"column {name} does not match the signed payload"
    return None


async def verify_chain(deps: AuditReadDeps, job_id: str | None = None) -> AuditVerifyResult:
    """Recompute every row from genesis: link, hash, signature and column/payload consistency."""
    key = load_signing_key(deps.settings)
    job_entry_ids: list[int] = []
    if job_id is not None:
        job_entry_ids = [
            r["id"]
            for r in await deps.db.fetch("SELECT id FROM audit_chain WHERE job_id = $1 ORDER BY id", job_id)
        ]
    expected_prev = GENESIS_HASH
    checked = 0
    async with deps.db.acquire() as conn:
        conn: asyncpg.Connection
        # One consistent snapshot even while other jobs keep appending.
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            async for row in conn.cursor(f"SELECT {_COLS} FROM audit_chain ORDER BY id", prefetch=500):
                checked += 1
                reason = check_row(row, expected_prev, key.fingerprint, key.verify)
                if reason is not None:
                    log.warning(
                        "audit_chain_broken", broken_at=row["id"], reason=reason, checked_rows=checked
                    )
                    return AuditVerifyResult(
                        ok=False,
                        checked_rows=checked,
                        broken_at=row["id"],
                        reason=reason,
                        head_hash=None,
                        job_entry_ids=job_entry_ids,
                    )
                expected_prev = row["payload_hash"]
    log.info("audit_chain_verified", checked_rows=checked, job_id=job_id)
    return AuditVerifyResult(
        ok=True,
        checked_rows=checked,
        head_hash=expected_prev if checked else GENESIS_HASH,
        job_entry_ids=job_entry_ids,
    )
