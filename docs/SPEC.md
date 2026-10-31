# VeriLoop — Original Product Specification (source of truth for requirements)

ROLE
You are the lead engineer and technical architect for "VeriLoop," a live hackathon prototype
in the Civic Pulse / Digital Governance & Public Information track. You will implement a
working, demo-ready system — not slides, not a mockup. Every component below is in scope
unless marked STRETCH.

OBJECTIVE
Build a high-throughput, agentic fact-verification and deepfake-audit platform that:
  1. Ingests a multimedia link (or uploaded file) from a social platform.
  2. Extracts the underlying claim (speech, caption, OCR text, on-screen text).
  3. Verifies it against authoritative news, fact-check registries, Wikidata, and forensic
     models in parallel.
  4. Emits a calibrated verdict with citations, or explicitly ABSTAINS.
  5. Closes the action gap: generates a platform report package and, for deepfake victims,
     a jurisdiction-aware legal draft — both requiring explicit human confirmation.
  6. Writes an immutable, hash-chained audit record and a signed PDF for every run.

USP (must be visible in the demo)
Existing tools stop at detection. VeriLoop owns the ENFORCEMENT LAYER:
verdict -> evidence bundle -> platform report -> legal escalation -> tamper-evident audit.
Do not build a chat wrapper. Build the closed loop.

## 1. AUTHORITATIVE TECH STACK (do not substitute without recording a decision)

| Layer | Choice | Rules |
|---|---|---|
| LLMs | Current-generation FAST models only. One primary. Second provider OPTIONAL (judge consensus only). | Resolve model IDs at runtime via the provider's list-models endpoint. Never hardcode a version string. Log the resolved ID in config and in every audit record. |
| News & evidence | Google News RSS; GDELT DOC 2.0 API; wire RSS (Reuters/AP/PTI/ANI); PIB RSS (India); Google Fact Check Tools API (ClaimReview); one general search API (Brave / Serper / Tavily — pick one). | Fast path = Google Fact Check (ClaimReview) first, then GDELT + wire RSS. Cache aggressively. |
| Reverse image | Google Cloud Vision "web detection"; Google Fact Check imageSearch; OWN pHash + CLIP index. | Own pHash index ALWAYS queried first (cheap, local, fast). |
| Forensics | Open-model ensemble (HF Inference or local) + ONE commercial deepfake-detection API. EXIF + C2PA metadata = BONUS signal only. | Ensemble vote. Define an ABSTAIN band explicitly (see §6). |
| Data | ONE vector store (pgvector OR Qdrant — choose one, justify in README) + Wikidata (SPARQL). | Neo4j is OPTIONAL and only for the spread/provenance graph (stretch). |
| Audit | Postgres with hash-chained rows + signed PDF export. | prev_hash chaining; PDF carries a SHA-256 digest + server signature. |
| Ingestion | Web app; Telegram bot; PWA share target. | Chrome MV3 extension and X bot are STRETCH ONLY — build last. |
| Deploy | ONE always-on host (Railway/Render/Fly/VPS). | Plus a pre-recorded backup demo video. No multi-region, no k8s. |

Model-resolution rule (mandatory): at boot, call the provider's model-listing endpoint, filter
to current fast/cheap tier, select the configured alias, and persist the resolved ID. If
resolution fails, fail fast with a clear log line — do not silently fall back to a stale name.

## 2. ARCHITECTURE

INGESTION
  Web app (React + Vite) -> POST /verify {url | upload, platform, locale}
  Telegram bot -> same internal endpoint
  PWA share_target -> same internal endpoint
  STRETCH: Chrome MV3 extension (context-menu "Verify with VeriLoop"), X bot (mention -> reply)

API GATEWAY (FastAPI)
  Auth (API key or session), rate limiting, request IDs, structured JSON logging.

ORCHESTRATOR (LangGraph state machine)
  Nodes:
    resolve_input      -> normalize URL, detect platform, fetch metadata (oEmbed / Playwright)
    extract_claim      -> ASR (Whisper-class), OCR (EasyOCR), caption parse, claim sentence split
    check_worthiness   -> lightweight classifier; gate the expensive pipeline
    fan_out            -> spawn evidence, forensic, provenance branches IN PARALLEL (async)
    fuse_evidence      -> weighted aggregation + calibration
    judge              -> Pro / Con / Judge debate; Verifier pass
    decide_action      -> verdict + confidence -> report eligibility
    build_report       -> evidence bundle assembly
    legal_draft        -> only if target = identifiable individual AND verdict = manipulated
    audit_write        -> hash-chained record + PDF
  Conditional edges:
    low check-worthiness        -> return "not checkable" (fast, cheap)
    any branch tool failure     -> retry (exp backoff x2) -> secondary provider -> mark
                                   branch "insufficient" (never invent evidence)
    fused confidence < threshold-> ABSTAIN node -> "Unverifiable — insufficient evidence"
    verifier finds ungrounded    -> reject verdict -> ABSTAIN with reason
    legal branch requested      -> HARD STOP for human confirmation

SERVICES (modular, independently testable)
  ingestion-service | orchestrator | forensic-service | evidence-service |
  provenance-service | fusion-service | reporting-service | legal-service | audit-service

DATA
  Postgres: jobs, verdicts, audit chain, report attempts, legal drafts (metadata only).
  Vector store (ONE): evidence chunks + claim embeddings + pHash/CLIP neighbors.
  Wikidata: SPARQL for entity grounding (people, orgs, places, events).
  Neo4j (OPTIONAL/STRETCH): spread graph (first-seen -> amplification edges).

## 3. EVIDENCE & PROVENANCE SPEC

FAST PATH (target < 3s): Google Fact Check Tools API
  - claimSearch (text) and imageSearch (by image URL/bytes)
  - If a matching ClaimReview exists with high similarity -> cite it directly.

DEEP PATH (target < 10s):
  - Google News RSS (query = extracted claim entities + keywords)
  - GDELT DOC 2.0 (timeline + tone + source diversity)
  - Wire RSS: Reuters, AP, PTI, ANI
  - PIB RSS for Indian government claims (flag government-source matches explicitly)
  - One general search API for long-tail coverage
  - Wikidata SPARQL: resolve entities, aliases, dates, offices, locations

PROVENANCE
  - Own pHash index: near-duplicate lookup against seeded known-fake corpus + prior runs.
  - Own CLIP index: semantic near-duplicate of frames.
  - Cloud Vision web detection: pages with matching/partial images, earliest timestamps.
  - Output "first-seen estimate" + list of prior appearances with timestamps.
  - If earliest occurrence is satire/state-affiliated/known-disinfo source -> surface as context.

RULES
  - Every evidence item stored with: source_name, url, published_at, retrieved_at,
    snippet, confidence_relevance, retrieval_method.
  - Minimum TWO independent citations required to emit True/False/Manipulated.
    One citation or fewer -> ABSTAIN.
  - Never synthesize a URL. Never cite a source not returned by a tool call.

## 4. FORENSIC SPEC

VISUAL / VIDEO
  - Frame sampling (e.g., 1 fps, cap N frames) — never process full video.
  - Open models: image/video deepfake detectors from HF (e.g., FaceForensics++-trained
    Xception/EfficientNet family, ViT-based detectors). Verify model IDs at build time.
  - One commercial API as a second opinion.
  - Ensemble rule: weighted vote. Record each model's raw score.

AUDIO
  - ASR transcript (Whisper-class). Language detection.
  - Cross-check transcript against on-screen text (OCR) — mismatch is a signal.
  - Optional: synthetic-speech classifier if available in the ensemble.

METADATA (bonus only)
  - EXIF presence/stripping anomalies, compression re-encode chains, container mismatches.
  - C2PA Content Credentials: if present, parse and report signer/issuer. Absence is NOT
    evidence of fakery — say so explicitly in the report.

ABSTAIN BAND (explicit)
  - If ensemble scores disagree by more than DELTA, or all scores fall in [LOW, HIGH]:
    forensic branch returns "inconclusive."
  - Inconclusive forensic + no strong textual evidence -> overall ABSTAIN.
  - Never convert a low-confidence forensic score into a "fake" verdict.

## 5. FUSION, JUDGE & CALIBRATION

FUSION
  - Weighted aggregation over: fact-check match, wire-news agreement/contradiction, GDELT
    corroboration, Wikidata consistency, forensic score(s), provenance signals.
  - Weights are configurable and printed in the audit record.
  - Produce a calibrated confidence (temperature scaling or simple isotonic map fitted on
    a small labeled set you assemble during the build).

MULTI-AGENT DEBATE (Tool-MAD pattern)
  - Proponent agent: argues claim is TRUE, tools = news RSS + GDELT + search.
  - Opponent agent: argues claim is FALSE, tools = Fact Check API + forensic + provenance.
  - Judge agent: sees both transcripts + evidence table, emits verdict + rationale.
  - Verifier agent: checks every sentence of the rationale is grounded in a stored evidence
    item. Any ungrounded sentence -> verdict rejected -> ABSTAIN.
  - Second LLM provider (optional): only used as an independent Judge; disagreement above
    threshold -> downgrade confidence or ABSTAIN.

VERDICT ENUM (fixed)
  TRUE | FALSE | MANIPULATED | UNVERIFIABLE | NOT_CHECKABLE
  MANIPULATED is reserved for synthetic/deepfake findings.
  UNVERIFIABLE is the abstain state. NOT_CHECKABLE is the cheap-gate output.

## 6. GUARDRAILS (hard requirements)
  G1. No citation -> no verdict.
  G2. Confidence below ABSTAIN_THRESHOLD -> UNVERIFIABLE.
  G3. Every tool failure is logged; no silent fallback that fabricates data.
  G4. Every verdict includes >= 2 independent sources OR is UNVERIFIABLE.
  G5. Reports and legal drafts are NEVER auto-submitted. Human confirmation gate.
  G6. Message content from Telegram is not persisted beyond a configurable TTL (default 24h);
      store anonymized verdict metadata only.
  G7. Resolved model IDs and tool versions are recorded in every audit record.
  G8. The UI must visually distinguish "no evidence found" from "evidence says false."
  G9. Any claim about a named private individual triggers extra caution: require forensic
      MANIPULATED + >=2 corroborating sources before legal draft is offered.

## 7. REPORTING AUTOMATION SPEC
  Adapters (each returns: platform, target_url, method, status, evidence_attached):
    - X: use public report flow (Playwright-driven, guided) unless an official endpoint exists.
    - Instagram / Facebook: Playwright-guided flow; user completes final submit.
    - YouTube: report flow via Playwright.
    - Telegram: no platform report API -> generate a shareable "Report Card" image + text
      with verdict, sources, QR to audit PDF.
    - WhatsApp: generate Report Card image + step-by-step forward instructions (no API).

  Evidence bundle (attached everywhere possible):
    verdict.json, sources.json, forensic_report.json, media hashes (SHA-256 + pHash),
    timestamps, audit_id, link to signed PDF.

  UX: "Prepare report" -> shows exactly what will be sent -> user clicks "I confirm" ->
  Playwright opens the platform flow pre-filled -> user submits. Log the attempt, not the
  user's identity.

## 8. LEGAL ESCALATION MODULE
  Trigger: verdict = MANIPULATED AND subject is an identifiable individual AND user requests it.
  Output: DRAFT ONLY. Never sent by the system.
  Templates (parameterized, jurisdiction selected by user):
    - India: IT Rules 2021 (impersonation / grievance officer) + DPDP Act erasure request.
    - EU: GDPR Art. 17 erasure request.
    - US: DMCA takedown (if applicable) + state NCII statutes note.
  Each draft includes: placeholders, evidence bundle reference, filing instructions,
  and a clear disclaimer that this is not legal advice.
  Require checkbox acknowledgements before the draft is generated.

## 9. AUDIT SPEC
  Postgres table audit_chain:
    id, created_at, prev_hash, payload_hash, payload_json, resolved_model_ids, tool_versions,
    signature
  Rule: payload_hash = SHA256(canonical_json(payload)); prev_hash = payload_hash of prior row.
  Export: GET /audit/{id}/pdf -> reportlab (or equivalent) PDF containing the full chain
  entry + SHA-256 digest + server signature (HMAC with server key or detached GPG signature).
  Verification endpoint: GET /audit/{id}/verify -> recomputes the chain and returns ok/fail.
  This endpoint is part of the live demo.

## 10. API CONTRACTS (minimum)
  POST /verify                 -> {job_id}
  GET  /verify/{job_id}        -> {status, verdict, confidence, sources[], forensic,
                                   provenance, abstain_reason?, audit_id}
  POST /report/{job_id}        -> {draft_package} (no submission)
  POST /report/{job_id}/confirm-> {attempt_logged: true}
  POST /legal/{job_id}/draft   -> {draft_text, jurisdiction, checklist[]}
  GET  /audit/{job_id}         -> {chain_entry}
  GET  /audit/{job_id}/pdf     -> signed PDF
  GET  /audit/{job_id}/verify  -> {ok: true|false, broken_at?}
  GET  /health                 -> {status, resolved_models, tool_status}

  All responses: JSON, request_id, latency_ms. Errors: RFC 7807 problem+json.

## 11. NON-FUNCTIONAL REQUIREMENTS
  Latency targets (live demo conditions):
    text/image claim, fast path:  < 5s
    video claim (sampled frames): < 20s
  Concurrency: handle 5 simultaneous demo requests without degradation.
  Resilience: circuit breaker per external tool; per-tool timeouts; retries with backoff;
    degraded-mode responses that name the failed tool.
  Observability: structured logs, per-run trace with node timings.
  Config: .env with all keys; no secrets in repo; .env.example provided.

## 12. EDGE CASES TO HANDLE GRACEFULLY (explicitly testable)
  E1. Unsupported platform link -> browser automation fallback -> if still failing,
      text-only analysis with a visible "visual forensics unavailable" label.
  E2. Login-walled media (Instagram) -> oEmbed/metadata -> if media inaccessible,
      analyze caption only and label it.
  E3. No claim in media (e.g., cooking Reel) -> NOT_CHECKABLE with explanation.
  E4. Fact Check API returns no match -> proceed to deep path; never treat absence as false.
  E5. Conflicting sources (wire says true, forensic says manipulated) -> ABSTAIN with
      both sides cited.
  E6. Tool outage mid-run -> degraded verdict or ABSTAIN; never crash the request.
  E7. Adversarially compressed media -> forensic abstain band -> UNVERIFIABLE.
  E8. Extremely long video -> frame cap + explicit "sampled" note.
  E9. Non-English claim -> detect language, attempt multilingual evidence, else ABSTAIN.
  E10. Rate-limited provider -> queue + "processing" status + polling URL.

## 13. DEMO REQUIREMENTS
  Live, end-to-end, on stage. Three scripted cases:
    (a) TRUE claim -> verdict with 2+ citations.
    (b) FALSE viral claim (e.g., fabricated missile-strike Reel) -> verdict + report package.
    (c) DEEPFAKE of an individual -> MANIPULATED + legal draft (never auto-sent).
  Show: orchestrator trace (node timings), abstain behavior on a deliberately ambiguous
  input, report pre-fill, and /audit/{id}/verify returning ok:true.
  Backup: pre-recorded video of the same three cases + a local fallback dataset so the
  demo survives network failure.

## 14. RUBRIC ALIGNMENT
  Engineering depth (20): modular services, typed contracts, real RAG + real forensics,
    async parallel branches, contract-first stubs.
  AI reasoning (25): LangGraph state machine, retries, circuit breakers, multi-agent debate,
    verifier pass, abstain band, calibrated confidence, source attribution.
  Relevance/USP (25): closed-loop enforcement, citizen-facing ingestion (Telegram + PWA),
    legal escalation, civic-data sources (PIB, GDELT, ClaimReview).
  Live demo (30): three scripted cases, backup recording, edge-case demos (abstain, outage),
    audit verification live.

## 15. HARD CONSTRAINTS / DO-NOTS
  D1. Do not hardcode LLM version strings. Resolve at runtime; log resolved IDs.
  D2. Do not add a second vector store or Neo4j before the stretch phase.
  D3. Do not build the Chrome extension or X bot before core + Telegram + PWA are green.
  D4. Do not auto-submit reports or legal notices.
  D5. Do not emit a verdict without citations.
  D6. Do not claim deepfake detection accuracy you did not measure on your own test set —
      report the measured numbers from your small labeled set.
  D7. Do not store user message content beyond TTL.

DELIVERABLES
  Runnable repo, README with architecture diagram + resolved model IDs + measured accuracy
  on your test set, .env.example, seed script for the pHash/known-fake index, demo script,
  backup recording, and a one-page rubric map.
