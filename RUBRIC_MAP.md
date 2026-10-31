# Rubric map (one page)

## Engineering depth: 20
| Criterion | Where to see it |
|---|---|
| Modular services | `backend/veriloop/services/*`: ingestion, extraction, evidence, provenance, forensic, fusion, debate, audit, reporting, legal. Each has its own tests. |
| Typed contracts, contract-first | `backend/veriloop/contracts/` (pydantic, `extra="forbid"`), mirrored in `frontend/src/api/types.ts` |
| Real RAG | ClaimReview index (Postgres FTS + multilingual-e5 rerank in pgvector), evidence embeddings, stance with exact-quote checks |
| Real forensics | 3 open models, pinned by revision, with a measured ensemble (`docs/media.md`); pHash + CLIP known-fake index; EXIF/C2PA |
| Async parallel branches | LangGraph fan-out: evidence ∥ forensic ∥ provenance, shown overlapping in the live trace |
| Ops | Embedded Postgres + pgvector, docker compose, one host; 600+ unit tests |

## AI reasoning: 25
| Criterion | Where to see it |
|---|---|
| State machine | `orchestrator/graph.py`, with conditional edges for not-checkable, cheap abstain and the debate path |
| Retries + circuit breakers | `core/resilience.py`, `core/tool_client.py`; live chaos switch `POST /admin/chaos` |
| Multi-agent debate | `services/debate`: proponent ∥ opponent → judge → dual-key verifier ∥ independent second judge (a different model) |
| Verifier pass | Every rationale sentence must cite stored evidence; deterministic + LLM + NLI checks (D-13) |
| Abstain band | Forensic LOW/HIGH/DELTA band; overall `UNVERIFIABLE` with 9 reason codes |
| Calibrated confidence | Platt scaling on our labelled set (`scripts/calibrate.py`, `config/calibration.json`) |
| Source attribution | Independent publisher groups (syndicated copies count once), government-source flag, the URL never synthesized |
| Model governance | Runtime model resolution with allowlist (D1); resolved ids in `/health` and every audit row |

## Relevance / USP: 25
| Criterion | Where to see it |
|---|---|
| Closed-loop enforcement | Verdict → evidence bundle (zip) → report card PNG + pre-filled platform report → legal draft → signed audit PDF |
| Citizen ingestion | PWA with an Android **share target**; Telegram bot (24 h TTL, HMAC'd chat ids) |
| Legal escalation | India (IT Rules 2021 Rule 3(2)(b), DPDP Act), EU (GDPR Art. 17), US (DMCA, TAKE IT DOWN Act); 4 mandatory acknowledgements; never sent |
| Civic data | PIB RSS (government-source flag), GDELT, the ClaimReview feed, Wikidata, Indian fact-checker lists |
| Works without paid keys | Keyless substitutes for every paid API (D-15) |

## Live demo: 30
| Moment | How |
|---|---|
| 3 scripted cases + abstain | `DEMO_SCRIPT.md`, `docs/demo_cases.md`; cases a/b/c/d |
| Orchestrator trace | Live Gantt from SSE in the job page, with per-node ms |
| Outage demo | `POST /admin/chaos {"tool":"gdelt","mode":"error"}`: degraded result naming the tool |
| Audit verification live | `/a/{id}` → **Verify chain** ✓; `scripts/tamper_demo.py` → ✗ broken_at N; `restore_chain.py` → ✓ |
| Survives network failure | `TOOL_MODE=record` then `TOOL_MODE=replay` (labelled REPLAY MODE) + backup video |
