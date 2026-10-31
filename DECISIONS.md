# Architecture decisions

Each entry records a choice where the spec allowed options or where we deviated from it, and why.
The full engineering detail is in [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) §2.

| ID | Decision | Why |
|---|---|---|
| D-01 | **LLM = aicredits.in** (OpenAI-compatible router), allowlist `gpt-6-luna`, `5.6-luna`. Ids are resolved at boot from `GET /models`, then checked with an authenticated probe call (the listing itself does not check the key). The process exits non-zero on failure. | Team-mandated provider. Spec D1 forbids hardcoded model ids, and the router's `created` field is constant, so ids are matched against the allowlist, never sorted by date. |
| D-01a | Strict `json_schema` structured outputs. A pydantic → strict-schema transform puts every property in `required` and sets `additionalProperties:false`. `reasoning_effort` is `none` for extraction and arguments and `low` for judge and verifier. No `temperature`. | Measured: effort `none` ≈ 1.5 s vs. 4.5 s at the default. Strict mode is rejected upstream unless every property is required. |
| D-01b | `LLM_PROVIDER=none` = explicit **extractive mode**: heuristics, NLI stance, quoted-evidence rationale, labelled in the UI and the audit. | The pipeline stays honest and usable without an LLM. |
| D-02 | Second judge = the other allowlisted model (`gpt-5.6-luna`). | Independent model generation for judge consensus; only these models are permitted. |
| D-03 | **pgvector** (in the single Postgres), not Qdrant. | One database holds jobs, evidence vectors, CLIP vectors, known fakes and the audit chain. That means one transaction boundary, one backup and one container on a single-host deploy, and the hash chain and its vectors live side by side. Collection sizes (≤ ~100k reviews, thousands of frames) are well within pgvector + HNSW. |
| D-04 | General search = Serper (keyed). Keyless substitutes: Google News RSS, Bing News RSS, Wikipedia. | Serper's `/news` returns dates. The keyless sources keep the demo working with zero keys. |
| D-05 | Commercial deepfake API = Sightengine (`deepfake` + `genai`). | Self-serve keys. Weighted 0.5 until measured. |
| D-06 | Modular monolith: one FastAPI process, each service a package with a typed interface and its own tests. | One always-on host (spec). Service boundaries are kept in code, not in network hops. |
| D-07 | In-process asyncio job runner backed by the Postgres `jobs` table, a semaphore of 6, and a bounded CPU pool for ML. | No Redis or Celery to operate. Queueing and the rate-limited status cover E10. |
| D-08 | Audit signature = **Ed25519**; public key at `/audit/pubkey`. | Anyone, including the browser, can verify the PDF and chain without a shared secret (HMAC could not do that). |
| D-09 | "Wire RSS" = Google News RSS with `site:` filters (Reuters, AP, PTI, ANI); PIB via its own RSS (`&reg=3`). | Reuters and AP no longer publish public RSS feeds. |
| D-10 | Platform reports: the server prepares the package; `reporter-agent/` (headed Playwright on the presenter's laptop, persistent profile) pre-fills the form and **stops before Submit**. | The server never holds platform credentials; a human always submits (G5/D4). |
| D-11 | Dev = embedded Postgres 16 + pgvector via `pgserver` (no Docker). Deploy = docker compose on one VPS. | Same SQL everywhere; zero-install local runs. |
| D-12 | One `ToolClient` "flight recorder" for every external call: timeouts, retries, circuit breakers, secondary tools, cache, chaos injection, record/replay cassettes. | Resilience requirements plus an offline demo backup (`TOOL_MODE=replay`, always labelled). |
| D-13 | Dual-key grounding: a rationale sentence passes only when the deterministic checks **and** the LLM support check **and** (when available) NLI entailment pass. | One model must not be able to approve its own output. |
| D-14 | Privacy: Telegram content lives only in TTL storage (24 h). Chat ids are stored as HMACs; audit payloads hold content hashes only. | G6/D7. |
| D-15 | **Keyless-first.** Every keyed integration is built and auto-activates; free substitutes run meanwhile (ClaimReview feed index, news RSS, pHash/CLIP index, local models). | The system must work end to end with zero paid keys. |
| D-16 | **The LLM is never a source.** It has no web access and stale knowledge, so it only reasons over evidence fetched in that job. | Verified: asked about October 2026 events, the model said they had not happened. |
