import asyncpg
import pytest

from veriloop.db.engine import Database

TABLES = {
    "jobs",
    "ephemeral_inputs",
    "evidence_items",
    "media_assets",
    "known_fakes",
    "forensic_scores",
    "report_attempts",
    "legal_drafts",
    "audit_chain",
    "tool_cache",
    "model_registry",
    "claimreview_index",
}


async def test_schema_applies_twice_idempotently(db: Database) -> None:
    await db.apply_schema()
    await db.apply_schema()
    rows = await db.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    assert TABLES <= {r["tablename"] for r in rows}
    assert await db.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    assert await db.ping()
    assert (await db.server_version()).startswith("16")


async def test_audit_chain_columns_match_build_spec(db: Database) -> None:
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'audit_chain' "
        "ORDER BY ordinal_position"
    )
    assert [r["column_name"] for r in rows] == [
        "id",
        "job_id",
        "event_type",
        "created_at",
        "prev_hash",
        "payload_hash",
        "payload_json",
        "resolved_model_ids",
        "tool_versions",
        "signature",
        "key_fingerprint",
    ]


async def test_audit_chain_trigger_blocks_update_delete_truncate(db: Database) -> None:
    row_id = await db.fetchval(
        "INSERT INTO audit_chain (job_id, event_type, prev_hash, payload_hash, payload_json, signature, "
        "key_fingerprint) VALUES ('j', 'verdict', $1, $2, '{}', 'sig', 'fp') RETURNING id",
        "0" * 64,
        "a" * 64,
    )
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await db.execute("UPDATE audit_chain SET payload_json = '{\"x\":1}' WHERE id = $1", row_id)
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await db.execute("DELETE FROM audit_chain WHERE id = $1", row_id)
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await db.execute("TRUNCATE audit_chain")
    assert await db.fetchval("SELECT payload_json FROM audit_chain WHERE id = $1", row_id) == "{}"


async def test_legal_drafts_and_report_attempts_have_no_text_or_identity_columns(db: Database) -> None:
    async def cols(table: str) -> set[str]:
        rows = await db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1", table
        )
        return {r["column_name"] for r in rows}

    legal = await cols("legal_drafts")
    assert "draft_sha256" in legal and not {"draft_text", "text", "body"} & legal
    attempts = await cols("report_attempts")
    assert not {"user_id", "email", "ip", "chat_id", "user_agent", "name"} & attempts


async def test_create_and_drop_database_helpers(db: Database) -> None:
    name = "vl_tmp_helper_check"
    await db.drop_database(name)
    dsn = await db.create_database(name)
    other = await Database.connect(dsn=dsn, min_size=1, max_size=1)
    try:
        await other.apply_schema()
        assert await other.fetchval("SELECT count(*) FROM jobs") == 0
    finally:
        await other.close()
        await db.drop_database(name)
    with pytest.raises(ValueError):
        await db.create_database('x"; DROP DATABASE postgres; --')
