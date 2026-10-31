# VeriLoop HTTP API

Owner: actions. Code: `backend/veriloop/api/` (`app.py` factory + lifespan, `context.py` request-id/latency
middleware, `problems.py` RFC 7807, `security.py` auth, `intake.py` JSON/multipart parsing, `spa.py` web app,
`routes/{verify,actions,audit,system}.py`). Contracts: `backend/veriloop/contracts/api.py` (field names are
exactly the contract's). Interactive schema: `GET /docs` (FastAPI).

Run: `cd backend && $PY -m veriloop` (uvicorn on `HOST:PORT`, default `127.0.0.1:8000`).

## Conventions (every endpoint)

* **Every JSON body** (success and error) gets `request_id` and `latency_ms`; every response also carries
  `X-Request-Id` and `X-Latency-Ms` headers. Send `X-Request-Id: <token>` (`[A-Za-z0-9._:-]{1,128}`) to
  choose the id; it is bound to every log line of the request.
* **Errors** are `application/problem+json`:
  `{"type","title","status","detail","instance","request_id","latency_ms"}` (+ `errors[]` for validation,
  `missing_acknowledgements[]` for legal). `type` is `about:blank` for plain HTTP errors or
  `urn:veriloop:problem:<name>` (`job-not-found`, `validation`, `rate-limited`, `unauthorized`,
  `report-not-eligible`, `legal-not-eligible`, `legal-acknowledgements`, `job-not-finished`, `too-large`,
  `unsupported-media`, `audit-not-found`, ...). Unhandled exceptions become a 500 problem (logged with the
  traceback under the same request_id; the message is not leaked).
* **Auth**: when `API_KEYS` is set, job/report/legal endpoints need `X-API-Key`. Same-origin browser
  requests (`Sec-Fetch-Site: same-origin`, or `Origin` = this server / `APP_BASE_URL`) are allowed without a
  key so the bundled web app and `EventSource` work; this is a convenience gate, the key is the boundary.
  Always public: `GET /health`, `GET /audit/*` (records must be verifiable by anyone holding a QR code),
  the web app, `POST /telegram/webhook` (own secret). Admin endpoints need `X-Admin-Key` = `ADMIN_KEY`.
* **Rate limit**: `RATE_LIMIT_PER_MIN` per client IP on `POST /verify` only (slowapi, in memory) -> 429
  problem with `Retry-After: 60`.
* **CORS**: `http://localhost:5173` and `http://127.0.0.1:5173` (Vite dev server).
* Job ids are 16-char Crockford base32 (`1m692fgehm936vs3`); anything else is a 404.

## Endpoints

| Method + path | Auth | Result |
|---|---|---|
| `POST /verify` | key / same-origin, rate limited | 202 `VerifyAccepted` + `Location` |
| `GET /verify/{id}` | key / same-origin | `JobView` (every field always present) |
| `GET /verify/{id}/events` | key / same-origin | SSE stream |
| `POST /report/{id}` | key / same-origin | `ReportDraftPackage` (prepare only, audited) |
| `POST /report/{id}/confirm` | key / same-origin | `ReportConfirmResponse` |
| `GET /report/{id}/attempts/{attempt_id}` | key / same-origin | confirmed plan for reporter-agent |
| `GET /report/{id}/card.png` | key / same-origin | PNG report card |
| `GET /report/{id}/bundle.zip` | key / same-origin | evidence bundle zip |
| `POST /legal/{id}/draft` | key / same-origin | `LegalDraftResponse` |
| `GET /audit/pubkey` | public | Ed25519 public key, PEM SPKI, `text/plain` |
| `GET /audit/{id}` | public | `AuditEntryView` (latest `verdict` row; `?event_type=` for others) |
| `GET /audit/{id}/pdf` | public | signed audit PDF |
| `GET /audit/{id}/verify` | public | `AuditVerifyResult` |
| `GET /health` | public | `HealthView` |
| `GET/POST /admin/chaos` | `X-Admin-Key` | inspect / inject tool faults |
| `POST /telegram/webhook` | Telegram secret header | only when `TELEGRAM_MODE=webhook` and the bot runs |
| `GET /*` | public | the web app (`frontend/dist`), index.html fallback |

### POST /verify

JSON `VerifyRequest` (what the web app sends):

```bash
curl -s -X POST http://127.0.0.1:8000/verify -H 'Content-Type: application/json' \
  -d '{"text":"The Union Cabinet approved a 3% hike in Dearness Allowance ... on 28 October 2026.","locale":"en-IN","source":"web"}'
```
```json
{"job_id":"1m692fgehm936vs3","status":"queued","status_url":"/verify/1m692fgehm936vs3",
 "events_url":"/verify/1m692fgehm936vs3/events","queue_position":0,
 "request_id":"57c6ebacd5514d20a81d4f151ba048aa","latency_ms":40}
```

Fields: `url?` (absolute http(s)), `text?` (<= 20 000 chars), `platform?` (contract enum; auto-detected when
omitted), `locale` (default `en-IN`), `source` (`web|pwa|api|extension`; `telegram` is reserved for the bot).
At least one of `url` / `text` / file. Unknown fields, `upload_id` and `source:"telegram"` are rejected (422).
JSON bodies are capped at 256 KB (413).

Multipart (file upload): `file` + the same fields as form fields (`platform` defaults to `upload`):

```bash
curl -s -X POST http://127.0.0.1:8000/verify -F file=@clip.mp4 -F locale=en-IN -F platform=upload -F source=web \
  -F 'text=This video shows Cyclone Arnab hitting Visakhapatnam.'
```

The file must be image/video/audio (415 otherwise), non-empty, <= `MAX_UPLOAD_MB` (413, checked on the
declared length and on the bytes copied). It is saved as `VERILOOP_HOME/media/<job_id>/upload<ext>` and the
job's `upload_id` is the job id. Multipart spooling uses `VERILOOP_HOME/tmp` (the app points Python's
`tempfile`, `TMP`, `TEMP` there at startup so nothing lands on C:).

### GET /verify/{id}

The full `JobView` (contracts/api.py); all fields are serialized, `null`/`[]` when not available yet. While
queued: `queue_position` + `status_detail` ("queued (2 job(s) ahead)"); while processing: the partial state
so far + `status_detail` "processing" or "processing (rate-limited: gdelt)" (E10). A job left unfinished by
a previous process is `failed` with `error` "interrupted by server restart".

### GET /verify/{id}/events (SSE)

Unnamed `data:` messages carrying JSON, each with `id: <seq>`; history first (resume with `Last-Event-ID`
or `?after=<seq>`), then live; the stream always ends with `{"type":"done",...}`. Keep-alive comments every 15 s.

```
id: 1
data: {"type":"status","status":"queued","queue_position":0,"status_detail":"queued (next to run)","seq":1}
id: 3
data: {"type":"node","node":"resolve_input","status":"start","seq":3}
id: 4
data: {"type":"node","node":"resolve_input","status":"end","duration_ms":3,"detail":"text/text; media 0","seq":4}
data: ... node events for every stage; skipped stages carry "detail":"skipped: <why>" ...
id: 24
data: {"type":"status","status":"completed","seq":24}
id: 25
data: {"type":"done","job_id":"1m692fgehm936vs3","status":"completed","verdict":"UNVERIFIABLE","audit_id":1,"seq":25}
```

Node `status` is `start|end|error` (+ `duration_ms`, optional `detail`). For a job that finished in an earlier
server process the stream is just a `status` and a `done` event built from the database.

### POST /report/{id} -> ReportDraftPackage

No body. For a completed FALSE/MANIPULATED verdict: `eligible:true`, `preview_text` (exactly what the person
reviews), `bundle` (`verdict`, `sources`, `forensic_report`, `media_hashes`, `timestamps`, `audit_id`, `pdf_url`,
`verify_url`), `bundle_url`, `platform_plans[]` (`platform`, `target_url`, `method`
`playwright_guided|report_card|deeplink_guided`, `steps`, `prefilled_text`, `report_category`, `card_url`) and the
`audit_id` of the REPORT_PREPARED row. Otherwise `eligible:false` + `ineligible_reason` (200). Nothing is sent.

### POST /report/{id}/confirm

```json
{"platform": "x", "confirmed": true}
```
-> `{"attempt_logged":true,"attempt_id":"ra_...","platform":"x","method":"playwright_guided",
"reporter_agent_command":"python reporter-agent/reporter_agent.py --api http://localhost:8000 --job <id> --attempt ra_...",
"audit_id":5,...}`. `confirmed` must be `true` (422), the platform must be one of the plans (422), the verdict must be
reportable (409). Logs a `report_attempts` row with no user identity and a REPORT_CONFIRMED audit row.
`reporter_agent_command` is `null` for `report_card` plans (Telegram/WhatsApp are manual by design).

`GET /report/{id}/attempts/{attempt_id}` returns the confirmed plan (`status:"confirmed"`, `plan`, `preview_text`,
`card_url`, `audit_page_url`, ...) that reporter-agent fetches; 404 for unknown/unconfirmed attempts.

`GET /report/{id}/card.png` (1080x1350 PNG: verdict, claim, top 2 sources, QR to `/a/{id}`) and
`GET /report/{id}/bundle.zip` (`verdict.json`, `sources.json`, `forensic_report.json`, `media_hashes.json`,
`report_card.png`, `audit_record.pdf`, `bundle.json` with per-file sha256, `README.txt`) need a completed job (409).

### POST /legal/{id}/draft

```json
{"jurisdiction": "IN",
 "acknowledgements": {"is_subject_or_authorized": true, "not_legal_advice": true,
                      "no_auto_send": true, "will_review_before_filing": true},
 "platform": null}
```
-> `LegalDraftResponse` (`draft_text`, `jurisdiction`, `checklist`, `filing_instructions`, `placeholders`,
`template_version`, `disclaimer`, `evidence_bundle_ref`, `audit_id`). Any acknowledgement false -> 422 with
`missing_acknowledgements`; not legal-eligible (MANIPULATED + identifiable individual, G9 for private/unknown
subjects) -> 409. The draft text is never stored (sha256 only) and never sent.

### Audit

* `GET /audit/{id}` -> `AuditEntryView`: `id, job_id, event_type, created_at, prev_hash, payload_hash,
  payload_json` (the canonical JSON text exactly as hashed), `resolved_model_ids, tool_versions,
  signature` (base64 Ed25519 over `"<prev_hash>:<payload_hash>"`), `key_fingerprint` (sha256 of the SPKI DER).
  404 until the run's VERDICT row is written. Browser check: `sha256(UTF-8(payload_json)) == payload_hash`
  (lowercase hex) and Ed25519-verify the signature with `GET /audit/pubkey`.
* `GET /audit/{id}/verify` -> `{"ok":true,"checked_rows":3,"broken_at":null,"reason":null,"head_hash":"...","job_entry_ids":[1]}`;
  after tampering: `{"ok":false,"checked_rows":1,"broken_at":1,"reason":"payload_hash does not match sha256(payload_json): the payload was modified",...}`.
  The whole chain is recomputed from genesis on every call.
* `GET /audit/{id}/pdf` -> `application/pdf` (inline).

### GET /health

`HealthView`: `status` ok|degraded, `mode` live|record|replay, `version`, `resolved_models`
(`{"llm_primary":"openai/gpt-6-luna","llm_secondary":"openai/gpt-5.6-luna"}`), `tool_status` (per registry tool:
`configured, state ok|degraded|open|unconfigured|disabled|chaos, version, calls, failures, last_error`), `db`, `ml`,
`llm_provider`.

### Admin chaos (live resilience demo)

```bash
curl -s -X POST http://127.0.0.1:8000/admin/chaos -H 'X-Admin-Key: change-me' -H 'Content-Type: application/json' \
  -d '{"tool":"gdelt","mode":"timeout"}'          # mode: timeout | error | off
# {"tool":"gdelt","mode":"timeout","active":{"gdelt":"timeout"},...}; /health shows gdelt "chaos"
```

Unknown tool / mode -> 422. `GET /admin/chaos` lists active faults.

## Web app serving

`frontend/dist` is served from `/`: existing files directly (`/assets/*` cached one year, `index.html`, `sw.js`,
`registerSW.js`, `manifest.webmanifest` with `Cache-Control: no-cache`; `.webmanifest` as
`application/manifest+json`, `.js` as `text/javascript`), any other non-API GET without a file extension gets
`index.html` (client routes `/`, `/j/{id}`, `/a/{id}`, `/share`, `/about`). API prefixes (`/verify`, `/report`,
`/legal`, `/audit`, `/health`, `/admin`, `/telegram`, `/docs`) never fall back to HTML. Without a build, `/`
returns a JSON hint.

## Lifespan

`bootstrap.startup` (fails fast on model resolution; exit code 3) -> job runner (marks interrupted jobs failed,
starts the rate-limit monitor) -> background: Telegram bot when `TELEGRAM_BOT_TOKEN` is set, TTL purge every
10 min, prewarm of service packages exposing `prewarm(deps)` when `ML_PREWARM=true` (imported in a worker
thread; missing packages are skipped) -> on shutdown: cancel background tasks, stop the bot, cancel running
jobs (marked failed), `bootstrap.shutdown`.

## Tests

`cd backend && $PY -m pytest tests/api -q` (37 tests: every endpoint, problem+json shape, request_id/latency_ms
on every JSON body, auth incl. same-origin, rate limit, chaos, multipart upload + limits, SSE live/replay/
Last-Event-ID/finished-elsewhere, SPA files + fallback + traversal, tamper -> broken_at -> restore, browser-side
audit check, Telegram hash-only audit, 500 problem).
