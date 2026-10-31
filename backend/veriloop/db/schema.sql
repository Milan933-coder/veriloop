-- VeriLoop schema. Idempotent: applied at every boot (Database.apply_schema) under an advisory lock.
-- Same SQL for embedded pgserver (dev/tests) and pgvector/pgvector:pg16 (deploy). Requires PG >= 14
-- (bit_count, CREATE OR REPLACE TRIGGER) and pgvector >= 0.5 (HNSW).
-- Changes: additive only (ADD COLUMN IF NOT EXISTS ...). Owned by foundation; request changes there.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------------------------
-- Jobs. result_json holds the JobView; state_json is free-form orchestrator scratch.
CREATE TABLE IF NOT EXISTS jobs (
    job_id           text PRIMARY KEY,
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
    source           text NOT NULL DEFAULT 'web',
    mode             text NOT NULL DEFAULT 'live',
    request_json     jsonb,              -- VerifyRequest; NULLed by Telegram TTL redaction (G6)
    state_json       jsonb,
    result_json      jsonb,
    verdict          text,
    confidence       double precision,
    audit_id         bigint,
    error            text,
    source_ref_hmac  text,               -- HMAC of e.g. a Telegram chat id; never the raw id (D-14)
    redacted         boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    completed_at     timestamptz,
    expires_at       timestamptz         -- content TTL deadline (Telegram)
);
CREATE INDEX IF NOT EXISTS jobs_status_created_idx ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS jobs_expires_idx ON jobs (expires_at) WHERE expires_at IS NOT NULL AND NOT redacted;

-- ---------------------------------------------------------------------------------------------
-- User-supplied content that must not outlive its TTL (G6/D7): purge_expired_ephemeral() deletes it.
CREATE TABLE IF NOT EXISTS ephemeral_inputs (
    id              bigserial PRIMARY KEY,
    job_id          text REFERENCES jobs (job_id) ON DELETE CASCADE,
    kind            text NOT NULL,       -- e.g. telegram_text | telegram_url | chat_ref | upload_path
    content         text NOT NULL,
    content_sha256  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ephemeral_inputs_expires_idx ON ephemeral_inputs (expires_at);
CREATE INDEX IF NOT EXISTS ephemeral_inputs_job_idx ON ephemeral_inputs (job_id);

-- ---------------------------------------------------------------------------------------------
-- Evidence items (citable pages). item_json is the full EvidenceItem contract.
CREATE TABLE IF NOT EXISTS evidence_items (
    pk                bigserial PRIMARY KEY,
    job_id            text NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    item_id           text NOT NULL,     -- E1.. / P1.. / D1.. (unique within a job)
    url               text NOT NULL,
    domain            text,
    publisher_group   text,
    source_name       text,
    title             text,
    snippet           text,
    published_at      timestamptz,
    retrieved_at      timestamptz NOT NULL DEFAULT now(),
    language          text,
    retrieval_method  text,
    tool              text,
    branch            text,
    stance            text,
    item_json         jsonb NOT NULL,
    embedding         vector(384),       -- multilingual-e5-small, L2-normalised
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, item_id)
);
CREATE INDEX IF NOT EXISTS evidence_items_url_idx ON evidence_items (url);
CREATE INDEX IF NOT EXISTS evidence_items_embedding_hnsw
    ON evidence_items USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------------------------
-- Media seen in jobs (images / sampled frames). Powers "prior run" near-duplicate matches.
CREATE TABLE IF NOT EXISTS media_assets (
    pk          bigserial PRIMARY KEY,
    job_id      text NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    asset_id    text NOT NULL,
    kind        text NOT NULL,
    sha256      text NOT NULL,
    phash       bit(64),
    clip        vector(512),
    frame_ts    double precision,
    width       integer,
    height      integer,
    source      text,
    path        text,                    -- local path under VERILOOP_HOME/media; never exposed via API
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, asset_id)
);
CREATE INDEX IF NOT EXISTS media_assets_sha256_idx ON media_assets (sha256);
CREATE INDEX IF NOT EXISTS media_assets_clip_hnsw ON media_assets USING hnsw (clip vector_cosine_ops);

-- ---------------------------------------------------------------------------------------------
-- Known fakes / reference media seeded from data/seed/known_fakes/manifest.csv.
CREATE TABLE IF NOT EXISTS known_fakes (
    id             text PRIMARY KEY,
    label          text NOT NULL CHECK (label IN ('manipulated', 'out_of_context', 'authentic_reference')),
    title          text,
    claim          text,
    notes          text,
    source_urls    jsonb NOT NULL DEFAULT '[]'::jsonb,   -- fact-check URLs from the manifest
    sha256         text,
    phash          bit(64),
    clip           vector(512),
    media_path     text,
    first_seen_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS known_fakes_sha256_idx ON known_fakes (sha256);
CREATE INDEX IF NOT EXISTS known_fakes_clip_hnsw ON known_fakes USING hnsw (clip vector_cosine_ops);

-- ---------------------------------------------------------------------------------------------
-- Per-model forensic scores (ModelScore contract) for audit/debugging.
CREATE TABLE IF NOT EXISTS forensic_scores (
    pk             bigserial PRIMARY KEY,
    job_id         text NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    asset_id       text,
    model_id       text NOT NULL,
    model_version  text NOT NULL DEFAULT '',
    provider       text NOT NULL,
    raw_score      double precision,
    weight         double precision NOT NULL DEFAULT 0,
    frames_scored  integer NOT NULL DEFAULT 0,
    latency_ms     integer NOT NULL DEFAULT 0,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS forensic_scores_job_idx ON forensic_scores (job_id);

-- ---------------------------------------------------------------------------------------------
-- Human-confirmed report attempts (G5). Deliberately no user identity columns.
CREATE TABLE IF NOT EXISTS report_attempts (
    attempt_id  text PRIMARY KEY,
    job_id      text NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    platform    text NOT NULL,
    method      text NOT NULL,
    status      text NOT NULL DEFAULT 'confirmed',
    target_url  text,
    audit_id    bigint,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS report_attempts_job_idx ON report_attempts (job_id);

-- ---------------------------------------------------------------------------------------------
-- Legal drafts: metadata + sha256 of the draft only. The draft text is never stored.
CREATE TABLE IF NOT EXISTS legal_drafts (
    draft_id          text PRIMARY KEY,
    job_id            text NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    jurisdiction      text NOT NULL,
    template_version  text NOT NULL,
    draft_sha256      text NOT NULL,
    platform          text,
    acknowledgements  jsonb NOT NULL DEFAULT '{}'::jsonb,
    audit_id          bigint,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS legal_drafts_job_idx ON legal_drafts (job_id);

-- ---------------------------------------------------------------------------------------------
-- Hash-chained, Ed25519-signed audit log (BUILD_SPEC §8). Append/verify logic: services/audit.
-- No FK to jobs on purpose: the chain must outlive any job row and can never be cascaded.
CREATE TABLE IF NOT EXISTS audit_chain (
    id                  bigserial PRIMARY KEY,
    job_id              text NOT NULL,
    event_type          text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    prev_hash           text NOT NULL,
    payload_hash        text NOT NULL,
    payload_json        text NOT NULL,   -- canonical JSON text exactly as hashed
    resolved_model_ids  jsonb NOT NULL DEFAULT '{}'::jsonb,
    tool_versions       jsonb NOT NULL DEFAULT '{}'::jsonb,
    signature           text NOT NULL,   -- base64 Ed25519 over "{prev_hash}:{payload_hash}"
    key_fingerprint     text NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_chain_job_idx ON audit_chain (job_id, event_type, id);

CREATE OR REPLACE FUNCTION audit_chain_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_chain is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE TRIGGER audit_chain_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_chain
    FOR EACH ROW EXECUTE FUNCTION audit_chain_append_only();

CREATE OR REPLACE TRIGGER audit_chain_no_truncate
    BEFORE TRUNCATE ON audit_chain
    FOR EACH STATEMENT EXECUTE FUNCTION audit_chain_append_only();

-- ---------------------------------------------------------------------------------------------
-- ToolClient response cache (second level behind the in-memory LRU).
CREATE TABLE IF NOT EXISTS tool_cache (
    tool        text NOT NULL,
    key_hash    text NOT NULL,
    op          text NOT NULL DEFAULT '',
    value       jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    PRIMARY KEY (tool, key_hash)
);
CREATE INDEX IF NOT EXISTS tool_cache_expires_idx ON tool_cache (expires_at);

-- ---------------------------------------------------------------------------------------------
-- Models resolved at boot (llm_primary, llm_secondary, ...): shown in /health and audit rows (G7).
CREATE TABLE IF NOT EXISTS model_registry (
    key          text PRIMARY KEY,
    provider     text NOT NULL,
    model_id     text NOT NULL,
    meta         jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Local index of Google's public ClaimReview feed (D-15, tool `claimreview_feed`).
-- Filled by scripts/sync_claimreview.py; queried FTS top-N -> embedding rerank.
-- 'simple' text search config: no stemming/stopwords, so Hindi and other languages index too.
CREATE TABLE IF NOT EXISTS claimreview_index (
    id                    text PRIMARY KEY,   -- stable_id(review_url, claim_text)
    claim_text            text NOT NULL,
    claimant              text,
    claim_date            timestamptz,
    review_url            text NOT NULL,
    publisher_name        text,
    publisher_site        text,
    rating_text           text,
    rating_value          double precision,
    best_rating           double precision,
    worst_rating          double precision,
    language              text,
    review_date           timestamptz,
    item_appearance_urls  text[] NOT NULL DEFAULT '{}',
    image_url             text,
    tsv                   tsvector GENERATED ALWAYS AS (
                              setweight(to_tsvector('simple'::regconfig, coalesce(claim_text, '')), 'A')
                              || setweight(to_tsvector('simple'::regconfig, coalesce(claimant, '')), 'B')
                          ) STORED,
    embedding             vector(384),        -- lazily filled (multilingual-e5-small, passage: prefix)
    synced_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claimreview_index_tsv_gin ON claimreview_index USING gin (tsv);
CREATE INDEX IF NOT EXISTS claimreview_index_embedding_hnsw
    ON claimreview_index USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS claimreview_index_review_url_idx ON claimreview_index (review_url);
CREATE INDEX IF NOT EXISTS claimreview_index_missing_embedding_idx
    ON claimreview_index (id) WHERE embedding IS NULL;
