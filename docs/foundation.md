# Foundation: shared backend core

Owner: foundation. Everything here is imported by every other package; the interfaces below are
stable. Requirements: `docs/SPEC.md`; build rules: `docs/BUILD_SPEC.md` §4 (core APIs), D-01, D-12, D-15, D-16.

## Running

```bash
PY=/c/Users/lokar/.veriloop/venv/Scripts/python.exe          # venv outside OneDrive
$PY -m pytest backend/tests/core backend/tests/db -q           # unit tests: no network, no downloads
$PY -m pytest backend/tests -q -m network -s                   # live aicredits smoke (needs .env key)
$PY -m pytest backend/tests -q -m ml -s                        # real Embedder/NLI (downloads once)
$PY -m ruff check backend
cd backend && $PY -m veriloop                                  # or the `veriloop` script; GET /health
```
`Makefile` has the same targets (`make test`, `make run`, ...) for machines with GNU make.

## Boot sequence (`veriloop.bootstrap.startup(settings) -> Deps`)

1. structlog JSON logging + secret redaction (every configured secret value is masked verbatim).
2. Directories under `VERILOOP_HOME` (`media/`, `models/`, `keys/`).
3. Postgres: `DATABASE_URL` or embedded pgserver at `VERILOOP_HOME/pgdata` (a separate postgres
   process that outlives ours, so restarts attach instantly); `schema.sql` applied under an
   advisory lock (idempotent); expired `tool_cache` rows purged.
4. `ToolClient` with the shared `httpx.AsyncClient`, two-level cache, chaos state, per-tool breakers.
5. LLM resolution (D-01), **fail fast**: `GET /models` -> allowlist match -> one ~15-token
   authenticated probe (`reasoning_effort: none`) -> persist `model_registry` -> log `model_resolved`.
   Any failure logs `model_resolution_failed` with the cause and the process exits non-zero
   (uvicorn exit code 3). `LLM_PROVIDER=none` (or no key) -> `DisabledLLM`, logged as `llm_disabled`.
6. `Embedder` / `NLI` objects (lazy: nothing loads until first use or a prewarm hook).
7. `EventBus`.

`shutdown(deps)` closes the event streams, HTTP client, DB pool and CPU pool.

## Public API (import paths)

```python
from veriloop.deps import Deps                     # settings, db, tools, llm, llm2, embedder, nli, events, resolved_models
from veriloop.bootstrap import startup, shutdown, build_health, build_llm_clients, build_deps_for_tests
from veriloop.core.config import Settings, get_settings, REPO_ROOT, default_home
from veriloop.core.tool_client import ToolClient, ToolCallResult, call_key_hash
from veriloop.core.tools_registry import TOOL_NAMES, ToolSpec, load_registry
from veriloop.core.errors import (ToolCallFailed, ToolAttemptError, LLMDisabled, ModelResolutionError,
                                  ModelNotAllowed, MLUnavailable, ProblemError)
from veriloop.core.llm import LLMClient, DisabledLLM, AicreditsLLM, Effort
from veriloop.core.strict_schema import to_strict_schema
from veriloop.core.ml import Embedder, NLI, NLIScore
from veriloop.core.executor import run_cpu
from veriloop.core.events import EventBus
from veriloop.core.chaos import ChaosState                 # deps.tools.chaos.set(tool, "timeout"|"error"|"off")
from veriloop.core.http import create_http_client, raise_for_status, user_agent
from veriloop.core.hashing import canonical_json, sha256_hex, stable_id
from veriloop.core.ids import new_job_id, new_id, is_job_id
from veriloop.core.logging import configure_logging, redact, redact_text
from veriloop.core.testing import FakeLLM, FakeEmbedder, FakeNLI, FakeClock, RecordingSleep
from veriloop.db.engine import Database
from veriloop.db import repo
```

### ToolClient (D-12 "flight recorder")

```python
result = await deps.tools.call("gnews_rss", "search", {"q": q, "hl": "en-IN"}, fetch)   # fetch: async () -> JSON
value  = await deps.tools.call_or_raise(...)          # returns value or raises ToolCallFailed(error)
```
Check order: unknown tool (ValueError) -> disabled/unconfigured (`unconfigured`, fn not called;
skipped in replay mode) -> chaos (`timeout`/`chaos`, message starts `chaos:`) -> replay
(`replay_miss`) -> cache -> breaker (`circuit_open`) -> attempts with per-attempt timeout and
retries (exp backoff 0.5 s x2 + 0-25 % jitter; `Retry-After` / `X-Ratelimit-Reset` honoured up to
15 s, longer waits fail fast as `rate_limited`) -> cache store + cassette (record mode).

* `fn` must return raw JSON (dict/list/str/number). Values are JSON-round-tripped so live, cached
  and replayed results are identical. Parse *after* the call. Never put a secret in `key`.
* Raise `ToolAttemptError(error_type, msg, retryable=..., retry_after_s=...)` inside `fn` to
  classify a failure yourself, or let httpx exceptions propagate (`raise_for_status` from
  `core.http` maps statuses: 429 -> `rate_limited`, 408/425/5xx -> retryable `http_error`,
  other 4xx -> non-retryable). Timeouts/transport errors are retryable; `ValueError` -> `parse_error`.
* Breaker: 5 transient failures in 60 s -> open 30 s -> half-open single probe (no retries).
  Non-transient errors (4xx, parse) mean "the service answered" and do not trip it.
* Cassettes: `data/fixtures/cassettes/<tool>/<sha256(op + ":" + canonical_json(key))>.json`,
  values and keys passed through `redact()`; strings over 2 KB in the key are stored as a hash.
* `status()` -> `dict[tool, ToolStatusView]` for /health: keyless tools `ok`/`degraded`, keyed tools
  without keys `unconfigured`, plus `open`, `chaos`, `disabled`. `tool_versions()` for audit rows.
  `secondary_for(tool)` lists configured, non-open fallbacks. `rate_limited_tools()` for E10.
* Tunables: `config/tools.yaml` (timeouts, TTLs, retries, secondaries, versions).

### LLM (D-01/D-01a/D-16)

```python
out = await deps.llm.complete_json(task="stance", system=SYS, user=text, schema=StanceOut,
                                   effort="none", max_tokens=2048, images=None)   # -> StanceOut
```
* Strict `json_schema` response_format from `to_strict_schema(schema)`; optional pydantic fields
  come back as `null`, fields with non-null defaults are required (not nullable). `dict` fields
  cannot be expressed in strict mode and will come back `{}`: use explicit models.
* Validation failure -> one repair round-trip -> `ToolCallFailed(parse_error)`. Refusal ->
  `ToolCallFailed(refusal)`. A 4xx naming `response_format`/`json_schema` -> one retry in
  `json_object` mode with the schema in the system prompt.
* `effort`: `none` for extraction/stance/proponent/opponent, `low` for judge/verifier.
* Every call is ToolClient tool `llm_primary` / `llm_secondary` (breaker, chaos, cassettes, status);
  `llm_call` log line per call with tokens, `cost_inr`, latency, request id, rate-limit remaining.
* Only ids resolved from `LLM_ALLOWED_MODELS` can be called (`ModelNotAllowed` otherwise).
* `DisabledLLM` (`provider == "none"`) raises `LLMDisabled`: take the extractive path.
* No search/plugin parameters exist: the LLM is never a source (D-16).

### Local ML

`await deps.embedder.embed(texts, kind="query"|"passage") -> np.ndarray (n, 384) float32, L2-normalised`
(e5 `query: `/`passage: ` prefixes added for you). `await deps.nli.score([(premise, hypothesis)])
-> list[NLIScore(entailment, neutral, contradiction)]`. Both lazy-load once on the CPU pool; check
`.available`; `MLUnavailable` when disabled/failed. `.status()`: disabled|idle|loading|loaded|unavailable.
`await x.load()` is the prewarm hook. Weights cache: `VERILOOP_HOME/models` (override with `ML_MODELS_DIR`).

### EventBus

`deps.events.publish(job_id, {...})` stamps `seq`; `async for e in deps.events.subscribe(job_id,
after_seq=last_event_id)` replays history then streams live; `deps.events.close(job_id)` ends all
streams (late subscribers replay and end). History is bounded (1000 events/job, 500 jobs LRU).

### Database (`veriloop.db.repo`)

All helpers take `db` (a `Database` or an asyncpg `Connection` from `async with db.transaction() as conn`).
JSON columns take dicts/pydantic models; vectors take numpy arrays; pHash is 16 hex chars.

| table | helpers |
|---|---|
| jobs | `create_job`, `get_job`, `update_job(**fields)`, `merge_job_state`, `list_jobs`, `queue_position`, `fail_interrupted_jobs`, `jobs_due_for_redaction` |
| ephemeral_inputs | `put_ephemeral(kind, content, ttl_s, job_id)`, `get_ephemeral`, `list_ephemeral`, `purge_expired_ephemeral(now=None)` |
| evidence_items | `insert_evidence_items(job_id, items, embeddings)`, `set_evidence_embeddings`, `list_evidence`, `search_evidence(vec, job_id/exclude_job_id, min_similarity)` |
| media_assets | `insert_media_assets(job_id, assets, clip=)`, `set_media_clip`, `list_media_assets`, `search_media_by_phash(hex, max_distance)`, `search_media_by_clip`, `find_media_by_sha256` |
| known_fakes | `upsert_known_fake(...)` (re-seed keeps hashes/vectors), `get_known_fake`, `list_known_fakes`, `count_known_fakes`, `find_known_fakes_by_sha256`, `search_known_fakes_by_phash`, `search_known_fakes_by_clip`; `KnownFakeHit.to_match()` -> contract `KnownFakeMatch` |
| forensic_scores | `insert_forensic_scores`, `list_forensic_scores` |
| report_attempts | `insert_report_attempt`, `set_report_attempt_audit`, `list_report_attempts` (no identity columns) |
| legal_drafts | `insert_legal_draft(draft_text=...)` stores only its sha256, `set_legal_draft_audit`, `list_legal_drafts` |
| tool_cache | `cache_get`, `cache_put`, `purge_expired_cache` (used by `ToolCache`) |
| model_registry | `upsert_model`, `get_model_rows`, `get_resolved_models` |
| claimreview_index | `ClaimReviewRecord`, `claimreview_id`, `upsert_claimreviews` (COPY + merge, batched), `search_claimreviews_fts` (any-word OR, `ts_rank`, returns stored embeddings), `search_claimreviews_by_embedding(vec, ids=)`, `claimreviews_missing_embeddings`, `set_claimreview_embeddings`, `claimreview_stats`, `get_claimreview` |
| audit_chain | table + append-only trigger (UPDATE/DELETE/TRUNCATE raise). Append/verify logic: services/audit |

Pgvector indexes are HNSW (cosine). pHash search is `bit_count(phash # query)` (sequential scan;
fine for tens of thousands of rows). ClaimReview FTS uses the `simple` config (no stemming, works
for Hindi); the query is tokenized by Postgres itself so it matches the indexed lexemes exactly.
Tests that need to tamper with `audit_chain` can `ALTER TABLE audit_chain DISABLE TRIGGER USER`
inside their own test database.

## Test fixtures (`backend/tests/conftest.py`)

`settings`, `make_settings(**overrides)`, `db` (session; fresh database on the shared embedded
server, dropped at the end; use unique ids), `fake_clock`, `fake_sleep`, `tool_client`, `fake_llm`,
`deps`. Put an `__init__.py` in every new test directory (test module names repeat across packages).

## Measured numbers (2026-10-31, this laptop, CPU only)

| what | result |
|---|---|
| unit tests (`backend/tests/core` + `backend/tests/db`, embedded PG 16) | 110 passed in ~8.5 s |
| model resolution at boot (list + probe) | ~4.0 s total, probe 2.1-2.8 s; resolved `openai/gpt-6-luna` + `openai/gpt-5.6-luna` |
| `complete_json`, gpt-6-luna, effort none, 82 prompt tokens | 3.3 s, INR 0.0025 |
| `complete_json`, gpt-5.6-luna, effort none | 1.4 s, INR 0.0054 |
| `complete_json` with the `JudgeOutput` contract schema (enum + `$defs`), gpt-6-luna, effort low | 1.8 s, INR 0.0062 (strict schema accepted upstream) |
| boot with `LLM_API_KEY=bad` | `model_resolution_failed` (probe: HTTP 401 Invalid API Key), exit code 3 |
| Embedder `intfloat/multilingual-e5-small` | first load 42.5 s incl. download; 4 texts 227 ms; query/passage cosine EN 0.897, HI 0.905, unrelated 0.713 |
| NLI `MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7` | not measured: the C: drive was full (os error 112) during the ~560 MB download; degraded to `unavailable` as designed. Rerun `ML_MODELS_DIR=D:/veriloop_models $PY -m pytest backend/tests -m ml -s` (or free space on C:) |

LLM latency varies run to run (the probe alone ranged 2.1-2.8 s); budget ~3 s per primary call at
effort none.
