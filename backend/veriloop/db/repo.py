"""Typed async helpers for every table services use (audit_chain logic lives in services/audit).

Every function takes `db` first: a `Database` (pooled) or an `asyncpg.Connection` (inside
`async with db.transaction() as conn:`). JSON columns take/return Python objects (codecs are set
on each connection); vector columns take numpy arrays or float lists. Perceptual hashes cross this
boundary as 16-char hex strings (MediaAsset.phash) and are converted to/from `bit(64)` in SQL.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

import asyncpg
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from veriloop.contracts import (
    EvidenceItem,
    JobStatus,
    KnownFakeMatch,
    MediaAsset,
    MediaKind,
    ModelScore,
)
from veriloop.core.hashing import normalize_for_json, sha256_hex, stable_id


class Executor(Protocol):
    async def execute(self, query: str, *args: Any) -> str: ...
    async def executemany(self, query: str, args: Any) -> Any: ...
    async def fetch(self, query: str, *args: Any) -> list[Any]: ...
    async def fetchrow(self, query: str, *args: Any) -> Any: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()  # "leave this column unchanged" (distinct from None = set NULL)

_HEX64 = re.compile(r"^[0-9a-fA-F]{16}$")
_PHASH_SQL = "('x' || {})::bit(64)"  # hex text -> bit(64)
_PHASH_HEX = "lpad(to_hex({}::bigint), 16, '0')"  # bit(64) -> hex text


def _phash(value: str | None) -> str | None:
    if value is None:
        return None
    if not _HEX64.fullmatch(value):
        raise ValueError(f"phash must be 16 hex chars, got {value!r}")
    return value.lower()


def _vec(value: Any, dim: int) -> np.ndarray | None:
    if value is None:
        return None
    arr = np.asarray(value, dtype=np.float32).reshape(-1)
    if arr.shape[0] != dim:
        raise ValueError(f"expected a {dim}-d vector, got {arr.shape[0]}")
    return arr


def _to_numpy(value: Any) -> np.ndarray | None:
    """pgvector's asyncpg codec returns `pgvector.Vector`; callers always get float32 numpy arrays."""
    if value is None:
        return None
    if hasattr(value, "to_numpy"):
        value = value.to_numpy()
    return np.asarray(value, dtype=np.float32)


def _jsonable(value: Any) -> Any:
    return None if value is None else normalize_for_json(value)


def _count(status: str) -> int:
    """'DELETE 3' / 'UPDATE 1' / 'INSERT 0 1' -> 3 / 1 / 1."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


# =============================================================================================
# jobs
# =============================================================================================
class JobRow(BaseModel):
    job_id: str
    status: JobStatus
    source: str
    mode: str
    request: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    verdict: str | None = None
    confidence: float | None = None
    audit_id: int | None = None
    error: str | None = None
    source_ref_hmac: str | None = None
    redacted: bool = False
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None


_JOB_SELECT = """
SELECT job_id, status, source, mode, request_json AS request, state_json AS state, result_json AS result,
       verdict, confidence, audit_id, error, source_ref_hmac, redacted, created_at, updated_at,
       started_at, completed_at, expires_at
FROM jobs
"""

# keyword -> column for update_job (whitelist: column names are never taken from callers)
_JOB_UPDATABLE = {
    "status": "status",
    "request": "request_json",
    "state": "state_json",
    "result": "result_json",
    "verdict": "verdict",
    "confidence": "confidence",
    "audit_id": "audit_id",
    "error": "error",
    "redacted": "redacted",
    "started_at": "started_at",
    "completed_at": "completed_at",
    "expires_at": "expires_at",
}
_JSON_COLS = {"request_json", "state_json", "result_json"}


async def create_job(
    db: Executor,
    job_id: str,
    *,
    request: Mapping[str, Any] | BaseModel | None = None,
    source: str = "web",
    mode: str = "live",
    status: JobStatus = JobStatus.QUEUED,
    expires_at: datetime | None = None,
    source_ref_hmac: str | None = None,
) -> JobRow:
    row = await db.fetchrow(
        """
        INSERT INTO jobs (job_id, status, source, mode, request_json, expires_at, source_ref_hmac)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING job_id, status, source, mode, request_json AS request, state_json AS state,
                  result_json AS result, verdict, confidence, audit_id, error, source_ref_hmac, redacted,
                  created_at, updated_at, started_at, completed_at, expires_at
        """,
        job_id,
        JobStatus(status).value,
        source,
        mode,
        _jsonable(request),
        expires_at,
        source_ref_hmac,
    )
    return JobRow.model_validate(dict(row))


async def get_job(db: Executor, job_id: str) -> JobRow | None:
    row = await db.fetchrow(_JOB_SELECT + " WHERE job_id = $1", job_id)
    return JobRow.model_validate(dict(row)) if row else None


async def update_job(db: Executor, job_id: str, /, **fields: Any) -> bool:
    """Update only the given columns (`update_job(db, id, status=..., result=view)`); bumps updated_at.

    Pass None to set NULL. JSON fields accept dicts or pydantic models. Returns False if no such job.
    """
    unknown = set(fields) - _JOB_UPDATABLE.keys()
    if unknown:
        raise ValueError(f"update_job: unknown field(s) {sorted(unknown)}")
    sets, args = [], [job_id]
    for name, value in fields.items():
        if value is UNSET:
            continue
        col = _JOB_UPDATABLE[name]
        if col == "status" and value is not None:
            value = JobStatus(value).value
        elif col in _JSON_COLS:
            value = _jsonable(value)
        args.append(value)
        sets.append(f"{col} = ${len(args)}")
    sets.append("updated_at = now()")
    status = await db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = $1", *args)
    return _count(status) == 1


async def merge_job_state(db: Executor, job_id: str, patch: Mapping[str, Any]) -> bool:
    """Shallow-merge `patch` into state_json (top-level keys replaced)."""
    status = await db.execute(
        "UPDATE jobs SET state_json = COALESCE(state_json, '{}'::jsonb) || $2::jsonb, updated_at = now() "
        "WHERE job_id = $1",
        job_id,
        _jsonable(dict(patch)),
    )
    return _count(status) == 1


async def list_jobs(
    db: Executor,
    *,
    status: JobStatus | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[JobRow]:
    rows = await db.fetch(
        _JOB_SELECT + " WHERE ($1::text IS NULL OR status = $1) AND ($2::text IS NULL OR source = $2)"
        " ORDER BY created_at DESC, job_id DESC LIMIT $3 OFFSET $4",
        JobStatus(status).value if status else None,
        source,
        limit,
        offset,
    )
    return [JobRow.model_validate(dict(r)) for r in rows]


async def queue_position(db: Executor, job_id: str) -> int:
    """Number of queued jobs ahead of this one (0 = next to run)."""
    return int(
        await db.fetchval(
            """
        SELECT count(*) FROM jobs q, jobs me
        WHERE me.job_id = $1 AND q.status = 'queued' AND (q.created_at, q.job_id) < (me.created_at, me.job_id)
        """,
            job_id,
        )
        or 0
    )


async def fail_interrupted_jobs(db: Executor, error: str = "interrupted by server restart") -> int:
    """At boot: jobs left queued/processing by a previous process can never finish; mark them failed."""
    status = await db.execute(
        "UPDATE jobs SET status = 'failed', error = $1, updated_at = now(), completed_at = now() "
        "WHERE status IN ('queued', 'processing')",
        error,
    )
    return _count(status)


async def jobs_due_for_redaction(db: Executor, *, now: datetime | None = None, limit: int = 500) -> list[str]:
    """Job ids whose content TTL (expires_at) has passed and that are not yet redacted (G6)."""
    rows = await db.fetch(
        "SELECT job_id FROM jobs WHERE expires_at IS NOT NULL AND NOT redacted "
        "AND expires_at <= COALESCE($1::timestamptz, now()) ORDER BY expires_at LIMIT $2",
        now,
        limit,
    )
    return [r["job_id"] for r in rows]


# =============================================================================================
# ephemeral_inputs (TTL-bound user content)
# =============================================================================================
class EphemeralInput(BaseModel):
    id: int
    job_id: str | None
    kind: str
    content: str
    content_sha256: str
    created_at: datetime
    expires_at: datetime


_EPH_COLS = "id, job_id, kind, content, content_sha256, created_at, expires_at"


async def put_ephemeral(
    db: Executor, *, kind: str, content: str, ttl_s: float, job_id: str | None = None
) -> EphemeralInput:
    row = await db.fetchrow(
        f"INSERT INTO ephemeral_inputs (job_id, kind, content, content_sha256, expires_at) "
        f"VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5)) RETURNING {_EPH_COLS}",
        job_id,
        kind,
        content,
        sha256_hex(content),
        float(ttl_s),
    )
    return EphemeralInput.model_validate(dict(row))


async def get_ephemeral(db: Executor, input_id: int) -> EphemeralInput | None:
    row = await db.fetchrow(
        f"SELECT {_EPH_COLS} FROM ephemeral_inputs WHERE id = $1 AND expires_at > now()", input_id
    )
    return EphemeralInput.model_validate(dict(row)) if row else None


async def list_ephemeral(db: Executor, job_id: str, *, kind: str | None = None) -> list[EphemeralInput]:
    rows = await db.fetch(
        f"SELECT {_EPH_COLS} FROM ephemeral_inputs WHERE job_id = $1 AND ($2::text IS NULL OR kind = $2) "
        "AND expires_at > now() ORDER BY id",
        job_id,
        kind,
    )
    return [EphemeralInput.model_validate(dict(r)) for r in rows]


async def purge_expired_ephemeral(db: Executor, *, now: datetime | None = None) -> int:
    """Delete content past its TTL. `now` lets tests (or a catch-up job) purge as of another time."""
    status = await db.execute(
        "DELETE FROM ephemeral_inputs WHERE expires_at <= COALESCE($1::timestamptz, now())", now
    )
    return _count(status)


# =============================================================================================
# evidence_items
# =============================================================================================
class EvidenceHit(BaseModel):
    job_id: str
    item: EvidenceItem
    similarity: float


async def insert_evidence_items(
    db: Executor,
    job_id: str,
    items: Sequence[EvidenceItem],
    embeddings: Sequence[Any] | np.ndarray | None = None,
) -> int:
    """Upsert items (by job_id + item.id). `embeddings[i]` (384-d) belongs to `items[i]`."""
    if embeddings is not None and len(embeddings) != len(items):
        raise ValueError("embeddings must align with items")
    records = []
    for i, item in enumerate(items):
        vec = _vec(embeddings[i], 384) if embeddings is not None else None
        records.append(
            (
                job_id,
                item.id,
                item.url,
                item.domain,
                item.publisher_group,
                item.source_name,
                item.title,
                item.snippet,
                item.published_at,
                item.retrieved_at,
                item.language,
                item.retrieval_method.value,
                item.tool,
                item.branch,
                item.stance.value,
                item.model_dump(mode="json"),
                vec,
            )
        )
    if not records:
        return 0
    await db.executemany(
        """
        INSERT INTO evidence_items (job_id, item_id, url, domain, publisher_group, source_name, title,
            snippet, published_at, retrieved_at, language, retrieval_method, tool, branch, stance,
            item_json, embedding)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        ON CONFLICT (job_id, item_id) DO UPDATE SET
            url = EXCLUDED.url, domain = EXCLUDED.domain, publisher_group = EXCLUDED.publisher_group,
            source_name = EXCLUDED.source_name, title = EXCLUDED.title, snippet = EXCLUDED.snippet,
            published_at = EXCLUDED.published_at, retrieved_at = EXCLUDED.retrieved_at,
            language = EXCLUDED.language, retrieval_method = EXCLUDED.retrieval_method, tool = EXCLUDED.tool,
            branch = EXCLUDED.branch, stance = EXCLUDED.stance, item_json = EXCLUDED.item_json,
            embedding = COALESCE(EXCLUDED.embedding, evidence_items.embedding)
        """,
        records,
    )
    return len(records)


async def set_evidence_embeddings(db: Executor, job_id: str, vectors: Mapping[str, Any]) -> int:
    records = [(job_id, item_id, _vec(vec, 384)) for item_id, vec in vectors.items()]
    if records:
        await db.executemany(
            "UPDATE evidence_items SET embedding = $3 WHERE job_id = $1 AND item_id = $2", records
        )
    return len(records)


async def list_evidence(db: Executor, job_id: str) -> list[EvidenceItem]:
    rows = await db.fetch("SELECT item_json FROM evidence_items WHERE job_id = $1 ORDER BY pk", job_id)
    return [EvidenceItem.model_validate(r["item_json"]) for r in rows]


async def search_evidence(
    db: Executor,
    query_vec: Any,
    *,
    limit: int = 10,
    job_id: str | None = None,
    exclude_job_id: str | None = None,
    min_similarity: float = 0.0,
) -> list[EvidenceHit]:
    """Cosine nearest neighbours over stored evidence (optionally within / excluding one job)."""
    rows = await db.fetch(
        """
        SELECT job_id, item_json, 1 - (embedding <=> $1) AS similarity
        FROM evidence_items
        WHERE embedding IS NOT NULL
          AND ($2::text IS NULL OR job_id = $2)
          AND ($3::text IS NULL OR job_id <> $3)
        ORDER BY embedding <=> $1
        LIMIT $4
        """,
        _vec(query_vec, 384),
        job_id,
        exclude_job_id,
        limit,
    )
    return [
        EvidenceHit(
            job_id=r["job_id"],
            item=EvidenceItem.model_validate(r["item_json"]),
            similarity=float(r["similarity"]),
        )
        for r in rows
        if float(r["similarity"]) >= min_similarity
    ]


# =============================================================================================
# media_assets
# =============================================================================================
class MediaHit(BaseModel):
    job_id: str
    asset_id: str
    sha256: str
    phash: str | None = None
    hamming: int | None = None
    similarity: float | None = None


async def insert_media_assets(
    db: Executor, job_id: str, assets: Sequence[MediaAsset], *, clip: Mapping[str, Any] | None = None
) -> int:
    """Upsert assets (by job_id + asset.id). `clip` maps asset id -> 512-d CLIP vector."""
    clip = clip or {}
    records = [
        (
            job_id,
            a.id,
            a.kind.value,
            a.sha256.lower(),
            _phash(a.phash),
            _vec(clip.get(a.id), 512),
            a.frame_ts,
            a.width,
            a.height,
            a.source,
            a.path,
        )
        for a in assets
    ]
    if not records:
        return 0
    phash_expr = _PHASH_SQL.format("$5::text")
    await db.executemany(
        f"""
        INSERT INTO media_assets (job_id, asset_id, kind, sha256, phash, clip, frame_ts, width, height,
                                  source, path)
        VALUES ($1, $2, $3, $4, CASE WHEN $5::text IS NULL THEN NULL ELSE {phash_expr} END,
                $6, $7, $8, $9, $10, $11)
        ON CONFLICT (job_id, asset_id) DO UPDATE SET
            kind = EXCLUDED.kind, sha256 = EXCLUDED.sha256, phash = EXCLUDED.phash,
            clip = COALESCE(EXCLUDED.clip, media_assets.clip), frame_ts = EXCLUDED.frame_ts,
            width = EXCLUDED.width, height = EXCLUDED.height, source = EXCLUDED.source, path = EXCLUDED.path
        """,
        records,
    )
    return len(records)


async def set_media_clip(db: Executor, job_id: str, asset_id: str, vector: Any) -> bool:
    status = await db.execute(
        "UPDATE media_assets SET clip = $3 WHERE job_id = $1 AND asset_id = $2",
        job_id,
        asset_id,
        _vec(vector, 512),
    )
    return _count(status) == 1


async def list_media_assets(db: Executor, job_id: str) -> list[MediaAsset]:
    rows = await db.fetch(
        f"""
        SELECT asset_id, kind, path, sha256, {_PHASH_HEX.format("phash")} AS phash, frame_ts, width, height,
               source
        FROM media_assets WHERE job_id = $1 ORDER BY pk
        """,
        job_id,
    )
    return [
        MediaAsset(
            id=r["asset_id"],
            kind=MediaKind(r["kind"]),
            path=r["path"] or "",
            sha256=r["sha256"],
            phash=r["phash"],
            frame_ts=r["frame_ts"],
            width=r["width"],
            height=r["height"],
            source=r["source"] or "download",
        )
        for r in rows
    ]


async def search_media_by_phash(
    db: Executor,
    phash_hex: str,
    *,
    max_distance: int = 10,
    exclude_job_id: str | None = None,
    limit: int = 20,
) -> list[MediaHit]:
    """Near-duplicate media from earlier jobs by pHash Hamming distance (popcount of XOR)."""
    q = _PHASH_SQL.format("$1::text")
    rows = await db.fetch(
        f"""
        SELECT job_id, asset_id, sha256, {_PHASH_HEX.format("phash")} AS phash,
               bit_count(phash # {q}) AS hamming
        FROM media_assets
        WHERE phash IS NOT NULL AND bit_count(phash # {q}) <= $2 AND ($3::text IS NULL OR job_id <> $3)
        ORDER BY hamming, pk DESC
        LIMIT $4
        """,
        _phash(phash_hex),
        max_distance,
        exclude_job_id,
        limit,
    )
    return [MediaHit(**dict(r)) for r in rows]


async def search_media_by_clip(
    db: Executor,
    vector: Any,
    *,
    limit: int = 10,
    min_similarity: float = 0.0,
    exclude_job_id: str | None = None,
) -> list[MediaHit]:
    rows = await db.fetch(
        f"""
        SELECT job_id, asset_id, sha256, {_PHASH_HEX.format("phash")} AS phash,
               1 - (clip <=> $1) AS similarity
        FROM media_assets
        WHERE clip IS NOT NULL AND ($2::text IS NULL OR job_id <> $2)
        ORDER BY clip <=> $1
        LIMIT $3
        """,
        _vec(vector, 512),
        exclude_job_id,
        limit,
    )
    return [MediaHit(**dict(r)) for r in rows if float(r["similarity"]) >= min_similarity]


async def find_media_by_sha256(
    db: Executor, sha256: str, *, exclude_job_id: str | None = None
) -> list[MediaHit]:
    rows = await db.fetch(
        f"""
        SELECT job_id, asset_id, sha256, {_PHASH_HEX.format("phash")} AS phash, 0 AS hamming
        FROM media_assets WHERE sha256 = $1 AND ($2::text IS NULL OR job_id <> $2) ORDER BY pk DESC
        """,
        sha256.lower(),
        exclude_job_id,
    )
    return [MediaHit(**dict(r)) for r in rows]


# =============================================================================================
# known_fakes
# =============================================================================================
class KnownFake(BaseModel):
    id: str
    label: str
    title: str | None = None
    claim: str | None = None
    notes: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    sha256: str | None = None
    phash: str | None = None
    media_path: str | None = None
    first_seen_at: datetime | None = None
    has_clip: bool = False


class KnownFakeHit(BaseModel):
    fake: KnownFake
    hamming: int | None = None
    clip_similarity: float | None = None

    def to_match(self) -> KnownFakeMatch:
        return KnownFakeMatch(
            known_fake_id=self.fake.id,
            label=self.fake.label,
            hamming=self.hamming,
            clip_similarity=self.clip_similarity,
            source_urls=self.fake.source_urls,
            title=self.fake.title,
            notes=self.fake.notes,
        )


_KF_SELECT = f"""
SELECT id, label, title, claim, notes, source_urls, sha256, {_PHASH_HEX.format("phash")} AS phash, media_path,
       first_seen_at, clip IS NOT NULL AS has_clip
"""


def _known_fake(row: Mapping[str, Any]) -> KnownFake:
    return KnownFake.model_validate({k: row[k] for k in KnownFake.model_fields})


async def upsert_known_fake(
    db: Executor,
    *,
    id: str,
    label: str,
    source_urls: Sequence[str] = (),
    title: str | None = None,
    claim: str | None = None,
    notes: str | None = None,
    sha256: str | None = None,
    phash: str | None = None,
    clip: Any = None,
    media_path: str | None = None,
    first_seen_at: datetime | None = None,
) -> None:
    """Insert or update a manifest entry. Missing hashes/vectors keep their stored values (re-seed safe)."""
    phash_expr = _PHASH_SQL.format("$8::text")
    await db.execute(
        f"""
        INSERT INTO known_fakes (id, label, title, claim, notes, source_urls, sha256, phash, clip, media_path,
                                 first_seen_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, CASE WHEN $8::text IS NULL THEN NULL ELSE {phash_expr} END,
                $9, $10, $11)
        ON CONFLICT (id) DO UPDATE SET
            label = EXCLUDED.label, title = EXCLUDED.title, claim = EXCLUDED.claim, notes = EXCLUDED.notes,
            source_urls = EXCLUDED.source_urls,
            sha256 = COALESCE(EXCLUDED.sha256, known_fakes.sha256),
            phash = COALESCE(EXCLUDED.phash, known_fakes.phash),
            clip = COALESCE(EXCLUDED.clip, known_fakes.clip),
            media_path = COALESCE(EXCLUDED.media_path, known_fakes.media_path),
            first_seen_at = COALESCE(EXCLUDED.first_seen_at, known_fakes.first_seen_at),
            updated_at = now()
        """,
        id,
        label,
        title,
        claim,
        notes,
        list(source_urls),
        sha256.lower() if sha256 else None,
        _phash(phash),
        _vec(clip, 512),
        media_path,
        first_seen_at,
    )


async def get_known_fake(db: Executor, fake_id: str) -> KnownFake | None:
    row = await db.fetchrow(_KF_SELECT + " FROM known_fakes WHERE id = $1", fake_id)
    return _known_fake(row) if row else None


async def list_known_fakes(db: Executor, *, label: str | None = None) -> list[KnownFake]:
    rows = await db.fetch(
        _KF_SELECT + " FROM known_fakes WHERE ($1::text IS NULL OR label = $1) ORDER BY id", label
    )
    return [_known_fake(r) for r in rows]


async def count_known_fakes(db: Executor) -> int:
    return int(await db.fetchval("SELECT count(*) FROM known_fakes") or 0)


async def find_known_fakes_by_sha256(db: Executor, sha256: str) -> list[KnownFake]:
    rows = await db.fetch(_KF_SELECT + " FROM known_fakes WHERE sha256 = $1 ORDER BY id", sha256.lower())
    return [_known_fake(r) for r in rows]


async def search_known_fakes_by_phash(
    db: Executor, phash_hex: str, *, max_distance: int = 10, limit: int = 10
) -> list[KnownFakeHit]:
    q = _PHASH_SQL.format("$1::text")
    rows = await db.fetch(
        _KF_SELECT
        + f""", bit_count(phash # {q}) AS hamming
        FROM known_fakes WHERE phash IS NOT NULL AND bit_count(phash # {q}) <= $2
        ORDER BY hamming, id LIMIT $3
        """,
        _phash(phash_hex),
        max_distance,
        limit,
    )
    return [KnownFakeHit(fake=_known_fake(r), hamming=int(r["hamming"])) for r in rows]


async def search_known_fakes_by_clip(
    db: Executor, vector: Any, *, limit: int = 10, min_similarity: float = 0.0
) -> list[KnownFakeHit]:
    rows = await db.fetch(
        _KF_SELECT
        + """, 1 - (clip <=> $1) AS similarity
        FROM known_fakes WHERE clip IS NOT NULL ORDER BY clip <=> $1 LIMIT $2
        """,
        _vec(vector, 512),
        limit,
    )
    return [
        KnownFakeHit(fake=_known_fake(r), clip_similarity=float(r["similarity"]))
        for r in rows
        if float(r["similarity"]) >= min_similarity
    ]


# =============================================================================================
# forensic_scores
# =============================================================================================
async def insert_forensic_scores(
    db: Executor, job_id: str, scores: Sequence[ModelScore], *, asset_id: str | None = None
) -> int:
    records = [
        (
            job_id,
            asset_id,
            s.model_id,
            s.model_version,
            s.provider,
            s.raw_score,
            s.weight,
            s.frames_scored,
            s.latency_ms,
            s.error,
        )
        for s in scores
    ]
    if records:
        await db.executemany(
            "INSERT INTO forensic_scores (job_id, asset_id, model_id, model_version, provider, raw_score, "
            "weight, "
            "frames_scored, latency_ms, error) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            records,
        )
    return len(records)


async def list_forensic_scores(db: Executor, job_id: str) -> list[ModelScore]:
    rows = await db.fetch(
        "SELECT model_id, model_version, provider, raw_score, weight, frames_scored, latency_ms, error "
        "FROM forensic_scores WHERE job_id = $1 ORDER BY pk",
        job_id,
    )
    return [ModelScore.model_validate(dict(r)) for r in rows]


# =============================================================================================
# report_attempts (no user identity is ever stored)
# =============================================================================================
class ReportAttemptRow(BaseModel):
    attempt_id: str
    job_id: str
    platform: str
    method: str
    status: str
    target_url: str | None = None
    audit_id: int | None = None
    created_at: datetime


_RA_COLS = "attempt_id, job_id, platform, method, status, target_url, audit_id, created_at"


async def insert_report_attempt(
    db: Executor,
    *,
    attempt_id: str,
    job_id: str,
    platform: str,
    method: str,
    status: str = "confirmed",
    target_url: str | None = None,
    audit_id: int | None = None,
) -> ReportAttemptRow:
    row = await db.fetchrow(
        f"INSERT INTO report_attempts (attempt_id, job_id, platform, method, status, target_url, audit_id) "
        f"VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING {_RA_COLS}",
        attempt_id,
        job_id,
        platform,
        method,
        status,
        target_url,
        audit_id,
    )
    return ReportAttemptRow.model_validate(dict(row))


async def set_report_attempt_audit(db: Executor, attempt_id: str, audit_id: int) -> bool:
    status = await db.execute(
        "UPDATE report_attempts SET audit_id = $2 WHERE attempt_id = $1", attempt_id, audit_id
    )
    return _count(status) == 1


async def list_report_attempts(db: Executor, job_id: str) -> list[ReportAttemptRow]:
    rows = await db.fetch(
        f"SELECT {_RA_COLS} FROM report_attempts WHERE job_id = $1 ORDER BY created_at", job_id
    )
    return [ReportAttemptRow.model_validate(dict(r)) for r in rows]


# =============================================================================================
# legal_drafts (metadata + sha256 only)
# =============================================================================================
class LegalDraftRow(BaseModel):
    draft_id: str
    job_id: str
    jurisdiction: str
    template_version: str
    draft_sha256: str
    platform: str | None = None
    acknowledgements: dict[str, Any] = Field(default_factory=dict)
    audit_id: int | None = None
    created_at: datetime


_LD_COLS = (
    "draft_id, job_id, jurisdiction, template_version, draft_sha256, platform, acknowledgements, audit_id, "
    "created_at"
)


async def insert_legal_draft(
    db: Executor,
    *,
    draft_id: str,
    job_id: str,
    jurisdiction: str,
    template_version: str,
    draft_text: str,
    acknowledgements: Mapping[str, Any] | BaseModel,
    platform: str | None = None,
    audit_id: int | None = None,
) -> LegalDraftRow:
    """Records that a draft was generated. Only sha256(draft_text) is stored, never the text."""
    row = await db.fetchrow(
        "INSERT INTO legal_drafts (draft_id, job_id, jurisdiction, template_version, draft_sha256, "
        "platform, "
        f"acknowledgements, audit_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING {_LD_COLS}",
        draft_id,
        job_id,
        jurisdiction,
        template_version,
        sha256_hex(draft_text),
        platform,
        _jsonable(acknowledgements),
        audit_id,
    )
    return LegalDraftRow.model_validate(dict(row))


async def set_legal_draft_audit(db: Executor, draft_id: str, audit_id: int) -> bool:
    status = await db.execute("UPDATE legal_drafts SET audit_id = $2 WHERE draft_id = $1", draft_id, audit_id)
    return _count(status) == 1


async def list_legal_drafts(db: Executor, job_id: str) -> list[LegalDraftRow]:
    rows = await db.fetch(
        f"SELECT {_LD_COLS} FROM legal_drafts WHERE job_id = $1 ORDER BY created_at", job_id
    )
    return [LegalDraftRow.model_validate(dict(r)) for r in rows]


# =============================================================================================
# tool_cache (used by core.cache.ToolCache)
# =============================================================================================
async def cache_get(db: Executor, tool: str, key_hash: str) -> tuple[Any, float] | None:
    """(value, remaining TTL seconds) for a live entry, else None."""
    row = await db.fetchrow(
        "SELECT value, EXTRACT(EPOCH FROM (expires_at - now()))::float8 AS remaining FROM tool_cache "
        "WHERE tool = $1 AND key_hash = $2 AND expires_at > now()",
        tool,
        key_hash,
    )
    return (row["value"], float(row["remaining"])) if row else None


async def cache_put(
    db: Executor, tool: str, key_hash: str, *, value: Any, ttl_s: float, op: str = ""
) -> None:
    await db.execute(
        """
        INSERT INTO tool_cache (tool, key_hash, op, value, expires_at)
        VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5))
        ON CONFLICT (tool, key_hash) DO UPDATE SET
            op = EXCLUDED.op, value = EXCLUDED.value, created_at = now(), expires_at = EXCLUDED.expires_at
        """,
        tool,
        key_hash,
        op,
        _jsonable(value),
        float(ttl_s),
    )


async def purge_expired_cache(db: Executor) -> int:
    return _count(await db.execute("DELETE FROM tool_cache WHERE expires_at <= now()"))


# =============================================================================================
# model_registry
# =============================================================================================
class ModelRegistryRow(BaseModel):
    key: str
    provider: str
    model_id: str
    meta: dict[str, Any] = Field(default_factory=dict)
    resolved_at: datetime


async def upsert_model(
    db: Executor, key: str, *, provider: str, model_id: str, meta: Mapping[str, Any] | None = None
) -> None:
    await db.execute(
        """
        INSERT INTO model_registry (key, provider, model_id, meta, resolved_at) VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (key) DO UPDATE SET provider = EXCLUDED.provider, model_id = EXCLUDED.model_id,
            meta = EXCLUDED.meta, resolved_at = now()
        """,
        key,
        provider,
        model_id,
        _jsonable(dict(meta or {})),
    )


async def get_model_rows(db: Executor) -> list[ModelRegistryRow]:
    rows = await db.fetch(
        "SELECT key, provider, model_id, meta, resolved_at FROM model_registry ORDER BY key"
    )
    return [ModelRegistryRow.model_validate(dict(r)) for r in rows]


async def get_resolved_models(db: Executor) -> dict[str, str]:
    return {r.key: r.model_id for r in await get_model_rows(db)}


# =============================================================================================
# claimreview_index (D-15: local index of Google's public ClaimReview feed)
# =============================================================================================
class ClaimReviewRecord(BaseModel):
    """One (claim, review) pair from the feed. `id` defaults to stable_id(review_url, claim_text)."""

    id: str = ""
    claim_text: str
    claimant: str | None = None
    claim_date: datetime | None = None
    review_url: str
    publisher_name: str | None = None
    publisher_site: str | None = None
    rating_text: str | None = None
    rating_value: float | None = None
    best_rating: float | None = None
    worst_rating: float | None = None
    language: str | None = None
    review_date: datetime | None = None
    item_appearance_urls: list[str] = Field(default_factory=list)
    image_url: str | None = None

    def model_post_init(self, _context: Any) -> None:
        if not self.id:
            self.id = claimreview_id(self.review_url, self.claim_text)


class ClaimReviewHit(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    record: ClaimReviewRecord
    rank: float = 0.0  # ts_rank for FTS hits
    similarity: float | None = None  # cosine similarity for embedding hits
    embedding: np.ndarray | None = None  # stored vector (None until backfilled) for client-side rerank


class ClaimReviewText(BaseModel):
    id: str
    claim_text: str
    language: str | None = None


class ClaimReviewStats(BaseModel):
    total: int
    with_embedding: int
    last_synced_at: datetime | None = None
    newest_review_date: datetime | None = None


def claimreview_id(review_url: str, claim_text: str) -> str:
    return stable_id(review_url.strip(), " ".join(claim_text.split()).lower())


_CR_FIELDS = (
    "id",
    "claim_text",
    "claimant",
    "claim_date",
    "review_url",
    "publisher_name",
    "publisher_site",
    "rating_text",
    "rating_value",
    "best_rating",
    "worst_rating",
    "language",
    "review_date",
    "item_appearance_urls",
    "image_url",
)
_CR_SELECT = ", ".join(_CR_FIELDS)
_CR_STAGE_DDL = """
CREATE TEMP TABLE IF NOT EXISTS _claimreview_stage (
    id text, claim_text text, claimant text, claim_date timestamptz, review_url text, publisher_name text,
    publisher_site text, rating_text text, rating_value float8, best_rating float8, worst_rating float8,
    language text, review_date timestamptz, item_appearance_urls text[], image_url text
) ON COMMIT DELETE ROWS
"""
_CR_MERGE = f"""
INSERT INTO claimreview_index ({_CR_SELECT}, synced_at)
SELECT DISTINCT ON (id) {_CR_SELECT}, now() FROM _claimreview_stage ORDER BY id
ON CONFLICT (id) DO UPDATE SET
    claim_text = EXCLUDED.claim_text, claimant = EXCLUDED.claimant, claim_date = EXCLUDED.claim_date,
    review_url = EXCLUDED.review_url, publisher_name = EXCLUDED.publisher_name,
    publisher_site = EXCLUDED.publisher_site, rating_text = EXCLUDED.rating_text,
    rating_value = EXCLUDED.rating_value, best_rating = EXCLUDED.best_rating,
    worst_rating = EXCLUDED.worst_rating,
    language = EXCLUDED.language, review_date = EXCLUDED.review_date,
    item_appearance_urls = EXCLUDED.item_appearance_urls, image_url = EXCLUDED.image_url, synced_at = now(),
    -- the embedding describes claim_text: drop it if the text changed so the backfill recomputes it
    embedding = CASE WHEN claimreview_index.claim_text IS DISTINCT FROM EXCLUDED.claim_text
                     THEN NULL ELSE claimreview_index.embedding END
"""


async def upsert_claimreviews(
    db: Any, records: Iterable[ClaimReviewRecord | Mapping[str, Any]], *, batch_size: int = 2000
) -> int:
    """Bulk upsert via COPY into a temp staging table + one merge per batch (fast for ~100k rows).

    `db` must be a Database (a connection is acquired) or an asyncpg Connection.
    Duplicate ids inside a batch are collapsed (last write is arbitrary but deterministic by id).
    """

    async def run(conn: asyncpg.Connection) -> int:
        total = 0
        batch: list[tuple[Any, ...]] = []

        async def flush() -> None:
            nonlocal total
            if not batch:
                return
            async with conn.transaction():
                await conn.execute(_CR_STAGE_DDL)
                await conn.copy_records_to_table(
                    "_claimreview_stage", records=batch, columns=list(_CR_FIELDS)
                )
                status = await conn.execute(_CR_MERGE)
            total += _count(status)
            batch.clear()

        for rec in records:
            r = rec if isinstance(rec, ClaimReviewRecord) else ClaimReviewRecord.model_validate(rec)
            batch.append(tuple(getattr(r, f) for f in _CR_FIELDS))
            if len(batch) >= batch_size:
                await flush()
        await flush()
        return total

    # Database and asyncpg.Pool hand out connections; a Connection (or pool proxy) is used directly so
    # the temp table, COPY and merge share one session.
    if not hasattr(db, "acquire"):
        return await run(db)
    async with db.acquire() as conn:
        return await run(conn)


async def _fts_query(db: Executor, text: str, max_terms: int = 32) -> str | None:
    """OR-tsquery of the query's distinct lexemes. Claims are phrased differently than reviews, so any
    shared word counts and ts_rank orders by overlap.

    Postgres tokenizes the query with the same parser/config as the indexed documents (Python's
    `\\w` would split Devanagari words at vowel signs), and the lexemes are cast straight to tsquery.
    """
    lexemes: list[str] = await db.fetchval("SELECT tsvector_to_array(to_tsvector('simple', $1))", text) or []
    terms = [lx for lx in lexemes if len(lx) > 1 and lx not in _STOPWORDS][:max_terms]
    if not terms:
        return None
    return " | ".join("'" + t.replace("\\", "\\\\").replace("'", "''") + "'" for t in terms)


# Tiny stopword list: the 'simple' config keeps every word, and OR-matching on "the"/"is"/"है" would
# match the whole index.
_STOPWORDS = frozenset(
    "the a an and or of to in on at for with by from is are was were be been it this that these those as "
    "not no yes but if then than so do does did has have had will would can could should may might its "
    "his her their our your my we you they he she i me him them us what which who whom when where why how "
    "all any some about into over after before also just only very more most said says say".split()
    + "है हैं था थे की के का को में से पर और या भी यह वह ये वो एक ने तो ही".split()
)


def _cr_hit(row: Mapping[str, Any], *, rank: float = 0.0, similarity: float | None = None) -> ClaimReviewHit:
    record = ClaimReviewRecord.model_validate({f: row[f] for f in _CR_FIELDS})
    emb = row["embedding"] if "embedding" in row.keys() else None
    return ClaimReviewHit(
        record=record,
        rank=rank,
        similarity=similarity,
        embedding=_to_numpy(emb),
    )


async def search_claimreviews_fts(
    db: Executor, query: str, *, limit: int = 50, language: str | None = None
) -> list[ClaimReviewHit]:
    """Top-`limit` reviews by full-text rank (any-word match), with stored embeddings for reranking."""
    tsquery = await _fts_query(db, query)
    if tsquery is None:
        return []
    rows = await db.fetch(
        f"""
        SELECT {_CR_SELECT}, embedding, ts_rank(tsv, q, 1) AS rank
        FROM claimreview_index, CAST($1 AS tsquery) AS q
        WHERE tsv @@ q AND ($2::text IS NULL OR language = $2)
        ORDER BY rank DESC, review_date DESC NULLS LAST
        LIMIT $3
        """,
        tsquery,
        language,
        limit,
    )
    return [_cr_hit(r, rank=float(r["rank"])) for r in rows]


async def search_claimreviews_by_embedding(
    db: Executor,
    query_vec: Any,
    *,
    limit: int = 20,
    ids: Sequence[str] | None = None,
    min_similarity: float = 0.0,
) -> list[ClaimReviewHit]:
    """Cosine search over embedded reviews; restrict to `ids` (e.g. the FTS candidates) to rerank."""
    rows = await db.fetch(
        f"""
        SELECT {_CR_SELECT}, embedding, 1 - (embedding <=> $1) AS similarity
        FROM claimreview_index
        WHERE embedding IS NOT NULL AND ($2::text[] IS NULL OR id = ANY($2::text[]))
        ORDER BY embedding <=> $1
        LIMIT $3
        """,
        _vec(query_vec, 384),
        list(ids) if ids is not None else None,
        limit,
    )
    return [
        _cr_hit(r, similarity=float(r["similarity"]))
        for r in rows
        if float(r["similarity"]) >= min_similarity
    ]


async def get_claimreview(db: Executor, review_id: str) -> ClaimReviewRecord | None:
    row = await db.fetchrow(f"SELECT {_CR_SELECT} FROM claimreview_index WHERE id = $1", review_id)
    return ClaimReviewRecord.model_validate(dict(row)) if row else None


async def claimreviews_missing_embeddings(db: Executor, *, limit: int = 512) -> list[ClaimReviewText]:
    rows = await db.fetch(
        "SELECT id, claim_text, language FROM claimreview_index WHERE embedding IS NULL ORDER BY id LIMIT $1",
        limit,
    )
    return [ClaimReviewText.model_validate(dict(r)) for r in rows]


async def set_claimreview_embeddings(db: Executor, vectors: Iterable[tuple[str, Any]]) -> int:
    records = [(review_id, _vec(vec, 384)) for review_id, vec in vectors]
    if records:
        await db.executemany("UPDATE claimreview_index SET embedding = $2 WHERE id = $1", records)
    return len(records)


async def claimreview_stats(db: Executor) -> ClaimReviewStats:
    row = await db.fetchrow(
        "SELECT count(*) AS total, count(embedding) AS with_embedding, max(synced_at) AS last_synced_at, "
        "max(review_date) AS newest_review_date FROM claimreview_index"
    )
    return ClaimReviewStats.model_validate(dict(row))
