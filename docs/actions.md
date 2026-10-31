# Actions: orchestrator, audit, reporting, legal, Telegram, reporter-agent

Owner: actions. HTTP surface: `docs/api.md`. Requirements: SPEC §7-10, guardrails G5/G6/G7/G9, D4/D7;
BUILD_SPEC §5, §6, §8, §9, D-08, D-10, D-14.

```
backend/veriloop/orchestrator/   graph.py (LangGraph), nodes.py (traced nodes), runner.py (jobs), jobview.py, services.py
backend/veriloop/services/audit/ chain.py (append/verify), keys.py (Ed25519), payload.py (VERDICT payload), pdf.py
backend/veriloop/services/reporting/  actions.py (prepare/confirm), plans.py, bundle.py, card.py, links.py
backend/veriloop/services/legal/      eligibility.py, drafting.py, templates/{in,eu,us}/{draft.txt.j2,meta.json}
backend/veriloop/bots/telegram/       bot.py (aiogram 3), flow.py (transport-agnostic), privacy.py (HMAC, TTL purge)
backend/scripts/                      tamper_demo.py, restore_chain.py, record_demo.py
reporter-agent/                       reporter_agent.py (CLI), agent_core.py, browser.py, selectors/*.json, tests/
```

## Orchestrator

`StateGraph(JobState)` exactly as BUILD_SPEC §6:

```
START -> resolve_input -> extract_claim -> check_worthiness
check_worthiness --(no claims / worthiness < CHECKWORTHINESS_THRESHOLD / upstream error)--> decide_action
check_worthiness --> evidence_branch, forensic_branch, provenance_branch      (one superstep: truly concurrent)
[3 branches] -> fuse_evidence
fuse_evidence --(candidate UNVERIFIABLE / NOT_CHECKABLE / upstream error)--> decide_action   (cheap abstain)
fuse_evidence --> judge (run_debate) -> decide_action -> build_report -> audit_write -> END
```

* **ServiceSet indirection** (`services.py`): a dataclass of the BUILD_SPEC §5 callables. `default_services()`
  imports `veriloop.services.<pkg>` lazily on first use, in a worker thread (the forensic package imports torch,
  ~11 s, which must not freeze the event loop / SSE). A missing module or function raises `ServiceUnavailable`
  inside the node. Tests inject fake ServiceSets.
* **traced(name)**: each node records a `NodeTiming` into `state.trace` (reducer), publishes
  `{"type":"node","node","status":"start|end|error","duration_ms","detail"?}` and logs `node_start/node_end`.
  Any exception -> node status `error` + `ToolError(tool="node:<name>", error_type="exception")`; downstream nodes
  route to `decide_action`, which emits **UNVERIFIABLE / TOOL_OUTAGE** naming the failed stage, and `audit_write`
  still records the run (E6). Nodes the graph never ran get `skipped: <reason>` timings so the Gantt is complete.
* **decide_action** calls `fusion.decide` (rule 1 NOT_CHECKABLE has a local fallback), then a defense-in-depth
  citation gate (G1/G4/D5: a decisive label needs >= 2 independent cited groups that exist in this job), then
  eligibility: report = FALSE/MANIPULATED; legal = MANIPULATED + identifiable individual (+ G9: private/unknown
  subject needs forensic MANIPULATED and >= 2 manipulation groups).
* **build_report** only summarises eligibility + bundle metadata. **Report and legal actions are never executed
  by the graph** (G5 hard stop): they run only from `/report/{id}/confirm` and `/legal/{id}/draft`.
* **audit_write** appends the VERDICT row (one retry); a failure is a node error, never a crash.
* **Runner** (`runner.py`, D-07): `submit()` creates the `jobs` row (queued) and an asyncio task gated by
  `Semaphore(MAX_CONCURRENT_JOBS)`; status events carry `queue_position` / `status_detail`; a 1 s monitor pushes
  `processing (rate-limited: <tool>)` from `tools.rate_limited_tools()` (E10); completion stores `state_json` +
  `result_json` (the JobView) and publishes `{"type":"done"}`, then closes the job's event stream. At startup
  queued/processing rows from a previous process are marked failed. Shutdown cancels running jobs (failed).
* **JobView** (`jobview.py`): every field populated; `sources` = cited first then the rest (evidence, provenance,
  debate follow-ups, deduplicated); `tool_errors` = node errors + every branch's errors; `degraded_tools` = registry
  tool names that really failed (`unconfigured` keyed tools are not "degraded", D-15); `labels` = mode labels
  (REPLAY MODE, LLM disabled ...) + every stage's caveats + "Pipeline stage failed: X — verdict withheld".

## Audit (D-08, BUILD_SPEC §8)

* Row = `job_id, event_type, created_at, prev_hash, payload_hash, payload_json, resolved_model_ids, tool_versions,
  signature, key_fingerprint`; table is append-only (trigger).
* `payload_json` = `core.hashing.canonical_json` (sorted keys, `(",",":")`, `ensure_ascii=False`, UTC ISO
  datetimes, floats rounded to 6 dp), stored as exact text. `payload_hash = sha256(payload_json)` (lowercase hex),
  `prev_hash` = previous row's payload_hash (genesis 64 zeros), `signature` = base64 Ed25519 over
  `"<prev_hash>:<payload_hash>"`. `append_event` adds `schema, job_id, event_type, recorded_at, resolved_model_ids,
  tool_versions` itself (callers cannot spoof them) -> **G7 in every row**, column and signed payload.
* Appends serialise on `pg_advisory_xact_lock` (read prev + insert in one transaction): 30 concurrent appends from
  5 jobs stay one linked chain (test).
* `verify_chain` recomputes every row from genesis in one repeatable-read snapshot: link, hash, signature (key
  fingerprint must be ours), canonical form, and column == payload consistency -> `{ok, checked_rows, broken_at,
  reason, head_hash, job_entry_ids}`.
* Key: `AUDIT_ED25519_KEY` (PKCS#8 PEM) or `VERILOOP_HOME/keys/audit_ed25519.pem`, generated once with O_EXCL.
  Public key at `/audit/pubkey` (PEM SPKI); fingerprint = sha256 of the SPKI DER. HKDF-derived sub-keys from it
  serve the Telegram chat-id HMAC and the webhook secret.
* VERDICT payload: verdict/label/confidence/abstain reason+detail/evidence_state, cited ids, rationale, evidence
  list (id, url, domain, group, published_at, retrieved_at, retrieval_method, tool + version, stance), media hashes
  (sha256 + pHash), forensic model scores, provenance matches, fusion signals + weights + versions, all thresholds,
  calibration version, debate summary, trace summary, tool errors, degraded tools, labels, mode.
* **Telegram jobs (D-14)**: `content_policy:"sha256_only"`: message text, URL, title, claim text, subject name,
  rationale sentences, `abstain_detail` and non-mode labels are stored only as sha256; trace details dropped.
* PDF (reportlab): verdict banner, summary, rationale, evidence table, forensic scores, provenance, models/tools,
  integrity block (payload sha256, prev hash, signature, key fingerprint, how to verify), QR to `APP_BASE_URL/a/<id>`,
  and the exact canonical payload as an appendix. Unicode font (Nirmala UI / DejaVu) so Hindi text is not boxes.

### Tamper demo (live)

```bash
cd backend
$PY scripts/tamper_demo.py --job <job_id>     # flips that job's VERDICT label to TRUE @ 0.99 in payload_json
# GET /audit/<job_id>/verify -> {"ok":false,"broken_at":<row id>,"reason":"payload_hash does not match ..."}
# the web app's /a/<job_id> "Verify chain" shows the broken chain; "Check in my browser" fails the hash check
$PY scripts/restore_chain.py                  # writes the saved original back -> ok:true again
```

The script connects as the embedded server's superuser, saves the original row to
`VERILOOP_HOME/audit_tamper_backup.json`, and in one transaction disables the append-only trigger, rewrites the
row, and re-enables it. `--rehash` also recomputes `payload_hash` (then the next row's link and the signature
fail); `--row N` targets a row id. It refuses to tamper twice without a restore.

## Reporting (SPEC §7, D-10)

* `prepare_report(job)`: only FALSE / MANIPULATED. Builds the evidence bundle (verdict.json, sources.json,
  forensic_report.json, media hashes, timestamps, audit_id, PDF + verify links), `preview_text` (exactly what will
  be reviewed), and plans: X / Instagram / Facebook / YouTube `playwright_guided` (deeplink when no URL), Telegram /
  WhatsApp `report_card` with forward instructions, anything else `deeplink_guided`. Appends REPORT_PREPARED with the
  preview sha256 and bundle file hashes (`submitted:false`).
* `confirm_report`: `confirmed:true` + a platform from the plans; inserts `report_attempts` (no identity columns
  exist and none are passed; Telegram target URLs are not stored), appends REPORT_CONFIRMED
  (`submitted_by_veriloop:false`) and returns the reporter-agent command.
* `render_report_card` (Pillow + qrcode, 1080x1350 PNG): verdict, claim, top 2 sources, QR to the audit page.
  `build_bundle_zip` adds the card, the signed PDF and a manifest with per-file sha256.

## reporter-agent (D-10, D4)

Standalone CLI (same venv): fetches `GET /report/{job}/attempts/{attempt}` (exists only for confirmed attempts),
opens the post in **headed** Chromium with a persistent profile at `VERILOOP_HOME/reporter-profile`
(`D:/veriloop/reporter-profile`), walks `selectors/<platform>.json` (click "More" -> "Report" -> wait for the dialog
-> paste the confirmed text where a text box exists), highlights the final Submit control and **stops**. It has no
code path that clicks a submit control: `validate_selectors` rejects any click step mentioning submit/send or
reusing a `final_submit` selector. Selector failure -> deep link + manual steps + the text to paste.
`report_card` / `deeplink_guided` plans are always manual. See `reporter-agent/README.md`.

## Legal (SPEC §8, G9, D4)

* Gate order: all four `LegalAcknowledgements` (else 422 with `missing_acknowledgements`) -> `legal_eligibility`
  (else 409) -> render the versioned Jinja2 template (`in-2026.10.1`, `eu-2026.10.1`, `us-2026.10.1`):
  India = IT Rules 2021 Rule 3(2)(b) grievance complaint (24-hour removal for impersonation incl. artificially
  morphed images) + DPDP Act 2023 correction/erasure request (with the note that DPDP Rules commence in phases) +
  cybercrime.gov.in note; EU = GDPR Art. 17 erasure request; US = DMCA 512(c) only if the person owns the
  copyright + TAKE IT DOWN Act (2025) note for intimate imagery + state NCII/deepfake statutes note.
* Every draft: `[FULL NAME]`-style placeholders for every victim detail, the evidence bundle reference (audit id,
  hashes, PDF link), filing instructions, a checklist and the disclaimer "This is a draft, not legal advice.
  VeriLoop never sends it. Verify the cited provisions with a lawyer."
* The LLM (effort none) may fill only two factual fields from the evidence bundle; every sentence must cite
  existing evidence ids and contain no URL, else the extractive text is used. Template-only when the LLM is off.
* `legal_drafts` stores metadata + sha256 only; LEGAL_DRAFT_GENERATED is appended (`sent:false`). No send code.

## Telegram (G6, D7, D-14)

* `start_telegram(deps, submit, wait)`: polling or webhook (`/telegram/webhook` + secret header) only when
  `TELEGRAM_BOT_TOKEN` is set; otherwise logs `telegram_unconfigured` and stays idle (current state: no token).
* Message with link / text / photo / video -> reply "Checking..." -> job with `source=telegram`, chat ref =
  HMAC-SHA256(chat id) with an HKDF key derived from the audit key, `expires_at = now + TELEGRAM_TTL_HOURS` ->
  edit the reply with verdict, top 2 sources, audit link, then send the report card.
* Raw message text/URL lives only in `ephemeral_inputs` (TTL); the job row's `request_json` holds only their
  sha256, `state_json` is not persisted for Telegram jobs, and the stored result view (claim, URL, needed for the
  "Full report" link) carries `expires_at`; media under `VERILOOP_HOME/media/<job>`. `purge_expired` (at boot and every 10 min) deletes expired ephemeral rows and
  uploads, then redacts due jobs (request/state dropped, claim/URL/title/debate removed from the stored view,
  media deleted, `redacted=true`). The audit chain only ever held hashes.

## Running

```bash
cd backend
$PY -m pytest tests/api tests/orchestrator tests/services/audit tests/services/reporting tests/services/legal tests/bots -q
$PY -m pytest ../reporter-agent/tests -q          # or from the repo root: $PY -m pytest reporter-agent/tests -q
$PY -m veriloop                                   # server; then scripts/record_demo.py drives the demo cases
```

## Measured (2026-10-31, this laptop, real `.env`)

| what | result |
|---|---|
| unit tests: api 37, orchestrator 21, audit 11, reporting 21, legal 21, bots 7 | 118 passed in ~60-110 s (machine load varies); reporter-agent 18 passed in 2 s |
| server boot to `/health` 200 (model resolution incl.) | ~6 s |
| first text claim (case a) while prewarm loaded models | 66 s wall, UNVERIFIABLE/LOW_CONFIDENCE (stance LLM timed out under CPU load; gdelt timeout); audit #1 verify ok |
| warm reruns (case a / case b, text) with the real debate package | 113 s / 98 s: evidence 27-34 s, judge 67-76 s; verdict UNVERIFIABLE/UNGROUNDED_RATIONALE (verifier rejected the judge's rationale; fusion candidates were TRUE / FALSE as expected) |
| tamper -> verify | `ok:false, broken_at:1` ("payload_hash does not match"); restore -> `ok:true` |
| artifacts | audit PDF 67 KB, card PNG 1080x1350 120 KB, bundle.zip 179 KB |
| headless Chromium | `/`, `/j/<id>`, `/a/<id>` render, 0 console errors / failed requests; in-browser SHA-256 and Ed25519 checks "matches" |

The orchestration overhead is milliseconds (fuse/decide/report/audit < 110 ms); the end-to-end latency is in the
evidence and debate stages (owned by the reasoning/media packages).
