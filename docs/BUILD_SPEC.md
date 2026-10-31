# VeriLoop — Build Spec (engineering source of truth)

Requirements live in `docs/SPEC.md`. This file says **how** we build them: environment, ownership,
interfaces, conventions. Typed contracts live in `backend/veriloop/contracts/` and are owned by the
architect — **do not edit contracts**; if a contract truly blocks you, work around it inside your
package and report the requested change in your final output.

---------------------------------------------------------------------------------------------------
## 1. Environment (Windows 11, read carefully)

* Repo root: `C:\Users\lokar\OneDrive\Desktop\veriloop_hakathon` (Git Bash: `/c/Users/lokar/OneDrive/Desktop/veriloop_hakathon`).
  The repo is inside **OneDrive** — never put venvs, model weights, databases, media or node portable
  installs inside the repo. Heavy/runtime state lives in `VERILOOP_HOME` = **`D:/veriloop`** (set in `.env`;
  pgdata, media, keys, models, datasets). **C: has < 2 GB free — never write large files to C:** (no
  downloads into `%TEMP%`, `~/.cache`, or the venv; use `settings.media_dir(job_id)` / `settings.models_dir`
  and pass explicit cache dirs to every ML library; `Settings.apply_model_cache_env()` already points
  HF_HOME / TORCH_HOME / EASYOCR_MODULE_PATH at `D:/veriloop/models`). The venv and portable Node stay at
  `C:/Users/lokar/.veriloop/{venv,node}`. The ClaimReview feed is at `D:/veriloop/datasets/claimreview/data.json`.
* Python: **3.12** via uv. Shared venv (outside OneDrive): `C:/Users/lokar/.veriloop/venv`.
  Run things with the venv interpreter directly:
  `PY=/c/Users/lokar/.veriloop/venv/Scripts/python.exe` then `$PY -m pytest backend/tests/...`.
  Backend package is installed editable (`uv pip install -e backend[ml,dev]`).
  **Do not install or upgrade packages** unless your task says you own dependency installation.
  If something is missing, lazy-import it, degrade gracefully (status `unavailable`), and list it
  under `missing_deps` in your final output.
* Node: portable Node LTS at `C:/Users/lokar/.veriloop/node` (prepend to PATH in each shell call).
* ffmpeg/ffprobe on PATH. git present. Docker installed but daemon NOT running — compose files are
  written but cannot be tested here.
* Postgres: **embedded via `pgserver`** (real Postgres 16 + pgvector, no Docker) when `DATABASE_URL`
  is empty; data dir `VERILOOP_HOME/pgdata`. Production uses `pgvector/pgvector:pg16` in compose.
  Same SQL everywhere.
* The only key present is `LLM_API_KEY` (aicredits.in) in the gitignored repo-root `.env`; no Google,
  Serper, Sightengine, HF or Telegram keys. Everything must run, and tests must pass, with zero keys.
  Keyed tools report `unconfigured` (never fake data). Keyless sources: Google News RSS, GDELT, PIB RSS,
  Wikidata, yt-dlp, local models.
* Many agents work in this repo **concurrently**. Only write files inside your owned paths (§3).
  Use Git Bash (`Bash` tool) or PowerShell; paths with spaces must be quoted.

---------------------------------------------------------------------------------------------------
## 2. Architecture decisions (also mirrored in DECISIONS.md)

| ID | Decision |
|---|---|
| D-01 | **LLM provider = aicredits.in** (team-mandated; earlier drafts misspelled it "airedis"), an **OpenAI-compatible router**: `LLM_BASE_URL=https://api.aicredits.in/v1`, `LLM_API_KEY` (in `.env` only, never in code/logs/cassettes/audit). **Only models in `LLM_ALLOWED_MODELS=gpt-6-luna,5.6-luna` may ever be called.** At boot: `GET {LLM_BASE_URL}/models` (Bearer auth; returns `{"object":"list","data":[...]}`, ~430 models). Verified 2026-10-31: allowed ids are listed as **`openai/gpt-6-luna`** and **`openai/gpt-5.6-luna`** (`is_active: true`, `supported_apis: ["chat_completions"]`, 1.05M context, image+text input). Match an id when its last `/`-segment equals the allowlist entry or ends with `-<entry>` (so `openai/gpt-5.6-luna` matches `5.6-luna`; `sao10k/l3-lunaris-8b` must NOT match). `created` is a constant placeholder (1700000000) so **never sort by created** — resolution is by allowlist match only, also requiring `is_active` when present. Primary = match for `LLM_PRIMARY_MODEL` (`gpt-6-luna`), second judge = match for `LLM_SECONDARY_MODEL` (`5.6-luna`). Persist in `model_registry`, log `model_resolved`, show in `/health`, put in every audit row. If `LLM_PROVIDER=aicredits` and resolution fails (DNS, auth, no allowed model listed) -> log `model_resolution_failed` with the cause and **exit non-zero**. Never a hardcoded model id in code (ids come from config + the listing). Any attempt to call a non-allowlisted id raises. |
| D-01a | Structured output (verified on both models): `POST {LLM_BASE_URL}/chat/completions` with `response_format={"type":"json_schema","json_schema":{"name":task,"strict":true,"schema":<strict schema>}}` and `max_completion_tokens`. Strict schema = pydantic schema transformed so every object has `additionalProperties:false` and all properties in `required` (optional fields become nullable). Keep a fallback to `{"type":"json_object"}` + schema in the system prompt if a 4xx names response_format; always validate with pydantic and allow one repair retry. Do not send `temperature`. **Effort → `reasoning_effort`**: `none` for extraction, stance, proponent/opponent; `low` for judge and verifier. Measured from this machine: gpt-6-luna `none` 1.5 s / `low` 3.0 s / default 4.5 s; gpt-5.6-luna `none` 1.4 s / `low` 1.9 s. Responses carry extra `message.reasoning` / `reasoning_details` fields — ignore them; read `choices[0].message.content`. `usage.cost` is reported (log it per call). |
| D-01b | `LLM_PROVIDER=none` is an explicit, logged offline mode ("extractive mode"): no LLM calls; claim split/worthiness by heuristics, stance by local NLI, rationale = quoted evidence sentences with citations; resolved model recorded as `none`. Default `LLM_PROVIDER=aicredits` if `LLM_API_KEY` set else `none` (logged loudly, shown in UI). The provider is reachable and the key in `.env` authenticates (HTTP 200 on `/models`). Unit tests still use `FakeLLM` (no network); mark live LLM tests `@pytest.mark.network`. |
| D-02 | Second judge = the other allowlisted model (`LLM_SECONDARY_MODEL`, default `5.6-luna`) on the same provider — an independent model, judge-consensus only. No other providers (Anthropic/Gemini/OpenAI) are allowed. |
| D-03 | Vector store = **pgvector** in the single Postgres (evidence embeddings 384-d, CLIP 512-d). No other vector store. |
| D-04 | General search = **Serper** (`/search`, `/news`). |
| D-05 | Commercial deepfake API = **Sightengine** (`deepfake` + `genai` models). |
| D-06 | Modular monolith: one FastAPI process; each service is a package with a typed interface + its own tests. |
| D-07 | Jobs: in-process asyncio runner, Postgres `jobs` table, `asyncio.Semaphore(MAX_CONCURRENT_JOBS)`; CPU/ML work via `core.executor.run_cpu` (bounded thread pool; models loaded once). |
| D-08 | Audit signature = Ed25519 (public key at `/audit/pubkey`). |
| D-09 | "Wire RSS" = Google News RSS with `site:` filters (reuters.com, apnews.com, ptinews.com, aninews.in) because Reuters/AP no longer publish public RSS. PIB via pib.gov.in RSS. |
| D-10 | Platform reports: server prepares; `reporter-agent/` (Playwright, headed, presenter laptop, persistent profile) pre-fills and STOPS before submit. |
| D-11 | Local dev = embedded Postgres (pgserver). Deploy = one VPS, docker compose (api + pgvector + caddy). |
| D-12 | Tool "flight recorder": every external call goes through `ToolClient` (timeouts, retry x2 exp backoff, circuit breaker, secondary provider, cache, chaos injection, record/replay cassettes). `TOOL_MODE=live|record|replay`. Replay mode is labeled everywhere ("REPLAY MODE"). |
| D-13 | Dual-key grounding: the verifier passes a rationale sentence only if deterministic checks pass AND (LLM verifier says supported) AND (local NLI entailment >= NLI_ENTAIL_MIN when NLI is available). |
| D-15 | **Keyless-first ("works with zero keys, gets better with keys").** Every keyed integration is fully built and auto-activates when its key appears in `.env`; until then a free, keyless substitute runs and the UI/audit say which one: **ClaimReview** → local index of Google's public ClaimReview feed (`https://storage.googleapis.com/datacommons-feeds/claimreview/latest/data.json`, ~200 MB, refreshed daily; verified 2026-10-31) loaded by `scripts/sync_claimreview.py` into Postgres (`claimreview_index` table: full-text `tsvector` + lazily-filled 384-d embeddings), queried hybrid (FTS top-50 → embedding rerank) as tool `claimreview_feed`; the Fact Check API (`factcheck`) is used in addition when `GOOGLE_FACTCHECK_API_KEY` is set. **General search** (Serper) → Google News RSS + Bing News RSS (`https://www.bing.com/news/search?q=...&format=rss`, tool `bing_news_rss`) + Wikipedia search API (tool `wikipedia`). **Reverse image** (Cloud Vision) → own pHash/CLIP index (seeded from known fakes + ClaimReview article images) + LLM visual query expansion (luna models accept images: describe the image → entities/claims → news queries). **Commercial deepfake API** (Sightengine) → local open-model ensemble only. **HF Inference** → local models (public weights need no token). **Telegram** → needs a BotFather token (cannot be automated); bot code is complete and idle until `TELEGRAM_BOT_TOKEN` is set. DuckDuckGo was tested and rejected (bot-challenge, no results). Note: Bing/Google RSS terms restrict redistribution — we display snippets with links back, demo/non-commercial use. |
| D-16 | **The LLM is never a source.** aicredits does not offer web search (plugins/`web_search_options` ignored; `:online` variants are not permitted for this key), and the luna models answer from stale training data (asked about Oct 2026 events, it claimed they had not happened). Therefore LLM output may only reason over evidence items returned by tools in this job; any factual sentence must cite an evidence id; the verifier rejects anything else. Never put model "knowledge" into evidence, stance or rationale. |
| D-14 | Privacy: Telegram content TTL (`TELEGRAM_TTL_HOURS=24`); audit payload stores `sha256` of user-supplied text/URL for Telegram jobs, never the content; chat ids stored only as HMAC. |

---------------------------------------------------------------------------------------------------
## 3. Repository layout & ownership

```
backend/
  pyproject.toml                       [foundation]
  veriloop/
    __init__.py  __main__.py  main.py  [foundation]   python -m veriloop  -> uvicorn
    deps.py                            [foundation]   Deps container
    contracts/                         [architect — read only]
    core/                              [foundation]   config, logging, ids, errors, resilience, tool_client,
                                                      tools_registry, http, cache, llm, model_registry,
                                                      ml (Embedder, NLI), executor, events, chaos
    db/                                [foundation]   schema.sql, engine.py (pgserver bootstrap), repo.py
    api/                               [api]          app factory, middleware, problem+json, routes/*
    orchestrator/                      [orchestrator] graph.py, nodes.py, runner.py, jobview.py
    services/
      ingestion/                       [ingest]
      extraction/                      [ingest]
      evidence/                        [evidence]
      provenance/                      [provenance]
      forensic/                        [forensic]
      fusion/                          [reasoning]
      debate/                          [reasoning]
      reporting/                       [actions]
      legal/                           [actions]
      audit/                           [audit]
    bots/telegram/                     [telegram]
  tests/
    conftest.py                        [foundation]   fixtures: settings, db (fresh database per session), fake_llm, tools, deps
    core/ db/                          [foundation]
    api/                               [api]
    orchestrator/                      [orchestrator]
    services/<name>/                   [owner of that service]
    bots/                              [telegram]
    edge_cases/                        [integration phase]
  scripts/                             each script owned by the service that needs it (named below)
config/                                weights.yaml, calibration.json [reasoning]; tools.yaml [foundation]
data/
  seed/known_fakes/manifest.csv        [provenance]
  lists/*.json                         [provenance]  satire, state_affiliated, known_disinfo, syndication
  eval/claims.jsonl                    [reasoning]
  eval/forensics/                      [forensic]
  fixtures/cassettes/                  recorded tool responses (record mode)
frontend/                              [frontend]
reporter-agent/                        [actions]
deploy/ Dockerfile docker-compose.yml Caddyfile  [foundation]
docs/                                  architect; agents may add docs/<area>.md for their area
```

---------------------------------------------------------------------------------------------------
## 4. Core APIs (implemented by foundation; every service uses only these)

```python
# veriloop/deps.py
@dataclass
class Deps:
    settings: Settings            # core.config.get_settings()
    db: Database                  # db.engine.Database (asyncpg pool + pgvector codec)
    tools: ToolClient             # core.tool_client
    llm: LLMClient                # primary (may be DisabledLLM)
    llm2: LLMClient | None        # optional independent judge
    embedder: Embedder            # core.ml — multilingual e5-small, 384-d, lazy
    nli: NLI                      # core.ml — multilingual NLI cross-encoder, lazy
    events: EventBus              # core.events — per-job pub/sub for SSE
    resolved_models: dict[str, str]
```

* `ToolClient.call(tool, op, key: dict, fn: async () -> JSONable, *, cache_ttl_s=None, timeout_s=None) -> ToolCallResult`
  `ToolCallResult{ok, value, error: ToolError|None, from_cache, replayed, latency_ms, attempts}`.
  **Tool fns return raw JSON-serializable payloads** (dict/list/str); parse in service code so
  record/replay works. Unconfigured tools return `ok=False, error_type="unconfigured"` without calling.
  `ToolClient.call_or_raise(...)` raises `core.errors.ToolCallFailed(error)`.
  Tool names are fixed in `core/tools_registry.py`:
  `factcheck, claimreview_feed, gnews_rss, bing_news_rss, wire_rss, pib_rss, gdelt, serper, wikipedia,
  wikidata, vision_web, sightengine, hf_inference, llm_primary, llm_secondary, oembed, page_fetch, ytdlp,
  playwright_fetch` (keyless: claimreview_feed, gnews_rss, bing_news_rss, wire_rss, pib_rss, gdelt,
  wikipedia, wikidata, oembed, page_fetch, ytdlp, playwright_fetch)
  (local models report through `/health.ml`, not ToolClient).
* `LLMClient.complete_json(*, task, system, user, schema: type[BaseModel], effort="low", max_tokens=2048) -> BaseModel`
  routed through ToolClient (`llm_primary` / `llm_secondary`). Raises `LLMDisabled` when provider is none,
  `ToolCallFailed` on failure/refusal/invalid JSON after repair. `llm.enabled`, `llm.provider`, `llm.model_id`
  attributes. OpenAI-compatible `POST {LLM_BASE_URL}/chat/completions` via httpx (D-01/D-01a). The API key
  must never appear in logs, errors, cassettes or audit payloads (redact `Authorization`).
* `Embedder.embed(texts, kind="query"|"passage") -> np.ndarray (n,384, L2-normalized)`; `.available`.
* `NLI.score(pairs: list[(premise, hypothesis)]) -> list[NLIScore(entailment, neutral, contradiction)]`; `.available`.
* `run_cpu(fn, *args)` — await CPU-bound work on the shared bounded pool.
* `EventBus.publish(job_id, dict)`, `subscribe(job_id) -> AsyncIterator[dict]`.
* `Database` helpers in `db/repo.py` (jobs, evidence_items, media_assets, known_fakes, report_attempts,
  legal_drafts, ephemeral_inputs, tool_cache, model_registry). Raw SQL via asyncpg; vectors via `pgvector.asyncpg`.
* `Settings.media_dir(job_id) -> Path` (created on demand), `Settings.home`.

---------------------------------------------------------------------------------------------------
## 5. Service interfaces (each owner implements exactly these; orchestrator/api call them)

```python
# services/ingestion/__init__.py
async def resolve_input(request: VerifyRequest, upload_path: str | None, job_id: str, deps: Deps) -> ResolvedInput
# services/extraction/__init__.py
async def extract_claims(resolved: ResolvedInput, locale: str, deps: Deps) -> ClaimExtraction
# services/evidence/__init__.py
async def gather_evidence(extraction: ClaimExtraction, resolved: ResolvedInput, locale: str, deps: Deps) -> BranchResult
async def followup_search(query: str, deps: Deps, *, start_index: int = 1) -> list[EvidenceItem]   # ids D{n}
# services/provenance/__init__.py
async def run_provenance(job_id: str, resolved: ResolvedInput, extraction: ClaimExtraction, deps: Deps) -> ProvenanceResult
# services/forensic/__init__.py
async def run_forensics(resolved: ResolvedInput, extraction: ClaimExtraction, deps: Deps) -> ForensicResult
# services/fusion/__init__.py          (pure, deterministic, no I/O)
def fuse(extraction, evidence: BranchResult, forensic: ForensicResult, provenance: ProvenanceResult, settings) -> FusionResult
def decide(extraction, fusion: FusionResult, debate: DebateResult | None, forensic, provenance, all_items: list[EvidenceItem], settings, *, llm_enabled: bool, degraded_tools: list[str]) -> Verdict
# services/debate/__init__.py
async def run_debate(extraction, fusion, items: list[EvidenceItem], forensic, provenance, deps) -> DebateResult
# services/audit/__init__.py
async def append_event(deps, job_id: str, event_type: AuditEventType, payload: dict) -> AuditEntryView
async def get_entry(deps, job_id: str, event_type: AuditEventType = VERDICT) -> AuditEntryView | None
async def verify_chain(deps, job_id: str | None = None) -> AuditVerifyResult
def render_pdf(entry: AuditEntryView, job: JobView | None, settings) -> bytes
def public_key_pem(settings) -> str
# services/reporting/__init__.py
async def prepare_report(job: JobView, deps) -> ReportDraftPackage
async def confirm_report(job: JobView, req: ReportConfirmRequest, deps) -> ReportConfirmResponse
def render_report_card(job: JobView, settings) -> bytes          # PNG
def build_bundle_zip(job: JobView, settings) -> bytes
# services/legal/__init__.py
def legal_eligibility(job: JobView) -> tuple[bool, str | None]
async def generate_legal_draft(job: JobView, req: LegalDraftRequest, deps) -> LegalDraftResponse
# bots/telegram/__init__.py
async def start_telegram(deps, submit: Callable[[VerifyRequest], Awaitable[str]]) -> None   # polling or webhook
async def purge_expired(deps) -> int
```

Every service package may expose `async def prewarm(deps) -> None` (load local models) — the app calls
them in the background at startup when `ML_PREWARM=true`. Every service must **never raise** to the
orchestrator for tool failures: return a result with `branch_status` INSUFFICIENT/FAILED and `errors`.

---------------------------------------------------------------------------------------------------
## 6. Orchestrator graph (LangGraph `StateGraph(JobState)`)

```
START -> resolve_input -> extract_claim -> check_worthiness
check_worthiness --(worthiness < CHECKWORTHINESS_THRESHOLD or no claims)--> decide_action
check_worthiness --> evidence_branch, forensic_branch, provenance_branch   (true parallel edges)
[evidence_branch, forensic_branch, provenance_branch] -> fuse_evidence
fuse_evidence --(fusion.candidate == UNVERIFIABLE)--> decide_action       (skip debate: cheap abstain)
fuse_evidence --> judge (run_debate: proponent ∥ opponent -> judge -> verifier ∥ second judge)
judge -> decide_action -> build_report -> audit_write -> END
```
* Each node is wrapped by `traced(name)`: records `NodeTiming`, publishes SSE events
  `{"type":"node","node":..,"status":"start|end|error","duration_ms":..}`; exceptions inside a node are
  caught -> node status `error`, a `ToolError(tool="node:<name>")`, and the job ends UNVERIFIABLE/TOOL_OUTAGE
  (still audited). The request never crashes (E6).
* Report/legal are **not** executed by the graph: `decide_action` only sets eligibility. They run only
  from `/report/{id}/confirm` and `/legal/{id}/draft` after human acknowledgement (G5, HARD STOP).
* `audit_write` appends `AuditEventType.VERDICT` with payload (§8) and stores `audit_id`.

---------------------------------------------------------------------------------------------------
## 7. Decision rules (implemented in `services/fusion`, tested exhaustively)

Signals (weights in `config/weights.yaml`, printed into every audit payload):

| signal | score s in [-1,1] | default weight |
|---|---|---|
| claimreview | normalized rating x similarity (only reviews with similarity >= 0.6) | 0.35 |
| news_stance | (support_groups - refute_groups) / max(1, stance_groups) over independent publisher groups | 0.25 |
| gdelt | corroboration in [0, 1]; **no coverage => absent, never negative** | 0.10 |
| wikidata | +1 consistent / -1 contradicted / absent | 0.10 |
| provenance | known-fake match -1; out-of-context -0.7*out_of_context_score; else absent | 0.20 |

T = sum(w*s*r)/sum(w) over **present** signals (absent signals excluded — E4/G8). p_true = Platt(T) from
`config/calibration.json`. M = max(conclusive forensic ensemble P(fake), 0.9 if known-fake(manipulated)
match, 0.9 if a ClaimReview with manipulation_rating and similarity >= 0.6).

`decide` order:
1. `NOT_CHECKABLE` if worthiness < threshold or no claims (E3) — explanation in abstain_detail.
2. CONFLICT (E5): T >= +0.5 with >= 2 supporting groups AND forensic status MANIPULATED -> UNVERIFIABLE/CONFLICT, cite both sides.
3. `MANIPULATED` iff (forensic MANIPULATED or known-fake manipulated match or manipulation ClaimReview) AND M >= MANIPULATION_MIN (0.8) AND groups_manipulation >= 2. Forensics alone never suffices.
4. TRUE if p_true >= ABSTAIN_THRESHOLD and groups_support >= 2 and groups_support >= 2*groups_refute.
   FALSE if (1 - p_true) >= ABSTAIN_THRESHOLD and groups_refute >= 2 and groups_refute >= 2*groups_support.
   Both sides with >= 2 groups each and neither dominating -> UNVERIFIABLE/CONFLICT citing both sides.
   Confidence = calibrated probability of the emitted label.
5. Otherwise UNVERIFIABLE with the most specific reason: NO_EVIDENCE (no relevant items at all),
   SINGLE_SOURCE (<2 groups), FORENSIC_INCONCLUSIVE, LOW_CONFIDENCE, TOOL_OUTAGE (key branches failed), LANGUAGE_UNSUPPORTED (E9).
6. After debate: judge label != fusion candidate -> UNVERIFIABLE/JUDGE_DISAGREEMENT; verifier not ok ->
   UNVERIFIABLE/UNGROUNDED_RATIONALE (show failing sentences); second judge disagrees -> confidence*0.7, abstain if < threshold.
7. G1/G4/D5 final gate: any non-abstain label must have >= 2 independent publisher groups among
   cited_evidence_ids and every cited id must exist in this job's items; otherwise UNVERIFIABLE.
8. Report eligible: FALSE or MANIPULATED. Legal eligible: MANIPULATED AND subject.is_identifiable_individual
   AND (if subject private or unknown: forensic MANIPULATED AND groups_manipulation >= 2) (G9).
`evidence_state` must distinguish NO_EVIDENCE from REFUTES (G8).

---------------------------------------------------------------------------------------------------
## 8. Audit (services/audit)

Table `audit_chain(id bigserial, job_id, event_type, created_at, prev_hash, payload_hash, payload_json text,
resolved_model_ids jsonb, tool_versions jsonb, signature, key_fingerprint)` + trigger rejecting UPDATE/DELETE.
`payload_json` holds the canonical JSON **text** (sorted keys, `separators=(",",":")`, `ensure_ascii=False`,
datetimes ISO-8601 UTC, floats rounded to 6 dp). `payload_hash = sha256(payload_json)`; `prev_hash` =
previous row's payload_hash (genesis = 64 zeros); `signature = Ed25519(f"{prev_hash}:{payload_hash}")`.
Append under `pg_advisory_xact_lock`. Payload contains: job_id, event_type, verdict/label/confidence/
abstain_reason, evidence (id, url, domain, published_at, retrieval_method), media hashes, weights,
thresholds, calibration version, resolved_model_ids, tool_versions, trace summary, mode; for Telegram
jobs only hashes of user content. Verify recomputes every row from genesis: hash, link, signature,
column/payload consistency -> `{ok, checked_rows, broken_at}`. Key: `AUDIT_ED25519_KEY` env (PEM) or
generated at `VERILOOP_HOME/keys/audit_ed25519.pem` on first boot.

---------------------------------------------------------------------------------------------------
## 9. API surface (api owner)

Spec endpoints (§10 of SPEC) plus: `GET /verify/{id}/events` (SSE), `GET /audit/pubkey`,
`POST /admin/chaos {tool, mode: timeout|error|off}` (X-Admin-Key), `GET /report/{id}/bundle.zip`,
`GET /report/{id}/card.png`, `POST /verify` accepts JSON **or** multipart (`file` + form fields).
Every JSON response body gets `request_id` + `latency_ms` (middleware). Errors: RFC 7807
`application/problem+json` `{type,title,status,detail,instance,request_id}`. Auth: `X-API-Key` when
`API_KEYS` set (web/PWA same-origin allowed with rate limit), admin endpoints need `X-Admin-Key`.
Rate limit `RATE_LIMIT_PER_MIN` per IP on POST /verify. SPA: serve `frontend/dist` with client routes
`/`, `/j/{id}`, `/share`, `/a/{id}`, `/about` (index.html fallback); API paths take precedence.
`POST /verify` returns **202** `VerifyAccepted`.

---------------------------------------------------------------------------------------------------
## 10. Config (`.env`, see `.env.example`)

```
VERILOOP_HOME=C:/Users/lokar/.veriloop   DATABASE_URL=            APP_BASE_URL=http://localhost:8000
API_KEYS=   ADMIN_KEY=change-me   RATE_LIMIT_PER_MIN=20   MAX_CONCURRENT_JOBS=6
LLM_PROVIDER=            # aicredits|none (auto: aicredits if LLM_API_KEY else none)
LLM_BASE_URL=https://api.aicredits.in/v1   LLM_API_KEY=
LLM_ALLOWED_MODELS=gpt-6-luna,5.6-luna   LLM_PRIMARY_MODEL=gpt-6-luna   LLM_SECONDARY_MODEL=5.6-luna
LLM_SECOND_JUDGE=true    LLM_TIMEOUT_S=20
GOOGLE_FACTCHECK_API_KEY=   GOOGLE_VISION_API_KEY=   SERPER_API_KEY=
SIGHTENGINE_API_USER=   SIGHTENGINE_API_SECRET=   HF_TOKEN=
TELEGRAM_BOT_TOKEN=   TELEGRAM_MODE=polling   TELEGRAM_WEBHOOK_SECRET=   TELEGRAM_TTL_HOURS=24
TOOL_MODE=live           # live|record|replay
ABSTAIN_THRESHOLD=0.70   CHECKWORTHINESS_THRESHOLD=0.40   MANIPULATION_MIN=0.80
FORENSIC_LOW=0.35  FORENSIC_HIGH=0.70  FORENSIC_DELTA=0.40   NLI_ENTAIL_MIN=0.50
FRAME_FPS=1  FRAME_CAP=16  AUDIO_CAP_S=90  MAX_UPLOAD_MB=50  MAX_VIDEO_S=900
ML_ENABLE=true  ML_PREWARM=true  ASR_MODEL=small
EMBED_MODEL=intfloat/multilingual-e5-small   NLI_MODEL=(verified at build time)
WIKIDATA_USER_AGENT=VeriLoop/0.1 (hackathon; contact: team@veriloop.local)
```

---------------------------------------------------------------------------------------------------
## 11. Conventions

* Python 3.12, type hints everywhere, pydantic v2, `async` I/O with `httpx.AsyncClient`, structlog JSON logs
  (`log.info("event_name", key=value)`), no print. Keep modules small and readable; comment the *why*.
* Tests: `pytest` + `pytest-asyncio` (`asyncio_mode=auto`), `respx` for HTTP mocking. **Default test run must
  not touch the network or download models**: mark such tests `@pytest.mark.network` / `@pytest.mark.ml`
  (deselected by default via `-m "not network and not ml"` in pytest config). Each owner adds at least one
  `network`/`ml` smoke test proving the real integration works, and runs it once.
* DB tests use the `db` fixture (fresh uniquely-named database on the shared embedded server, dropped after).
* Never synthesize URLs, sources, scores or model ids. If a tool fails: `ToolError`, status, move on.
* All user-visible caveats go into `labels` (e.g. `Visual forensics unavailable — analyzed caption only`,
  `Sampled 16 frames of 412 s video`, `REPLAY MODE`, `LLM disabled — extractive rationale`).
* Definition of done for a package: interface implemented, unit tests green
  (`$PY -m pytest backend/tests/services/<name> -q`), one real smoke test run (network/ml) with result
  reported, `ruff check` clean for your files, a short `docs/<area>.md` describing design + measured numbers.
