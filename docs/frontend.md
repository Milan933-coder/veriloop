# VeriLoop frontend (web app + PWA)

Vite 7 + React 19 + TypeScript (strict) + Tailwind 4 + react-router 7 + vite-plugin-pwa. Lives in `frontend/`.
`npm run build` -> `frontend/dist` (FastAPI serves it at `/` with an index.html fallback).

## Run

All commands from `frontend/`, with `export PATH="/c/Users/lokar/.veriloop/node:$PATH"` (Git Bash).

| Command | What it does |
|---|---|
| `npm install` | installs exact pinned versions (lockfile committed) |
| `npm run dev` | dev server on :5173, proxies `/verify /report /legal /audit /health /admin /upload*` to `http://localhost:8000` (override with `VERILOOP_API=`) |
| `npm run dev:mock` | same, but every API call is served by in-browser fixtures (`VITE_MOCK=1`, see `.env.mock`) |
| `npm run dev` + `?mock=1` | turn mock mode on for one tab in dev (`?mock=0` turns it off) |
| `npm run build` | `tsc --noEmit` + production build into `dist/` (fixtures are compiled out) |
| `npm run build:mock` | a demo-prep build that includes fixtures (always shows the MOCK DATA banner) |
| `npm test` | Vitest (node environment, no jsdom) |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run icons` | regenerates the PWA icons (`scripts/gen-icons.mjs`, zero deps) |

Mock-mode knobs (URL params): `?sse=fail` (drop the event stream to exercise polling), `?health=replay|extractive|degraded`
(mode banners), `?tamper=1` (audit verify returns a broken chain). The submit page shows a scenario picker in mock mode.

## Routes and components

| Route | Page | Main pieces |
|---|---|---|
| `/` | `SubmitPage` | Link / upload (<= 50 MB) / text; platform auto-detected (editable); locale (en-IN default); examples; install hint |
| `/j/:id` | `JobPage` | `StatusPanel` (queued, processing, rate-limited), `VerdictCard`, `TraceTimeline`, `EvidenceList`, `ForensicPanel`, `ProvenancePanel`, `ReasoningPanel`, `ReportFlow`, `LegalWizard`, audit link |
| `/a/:id` | `AuditPage` | chain entry, **Verify chain** (big ok/broken result), in-browser SHA-256 + Ed25519 check, PDF + public key, QR |
| `/share` | `SharePage` | PWA share target: extracts link/text, one tap "Verify now" |
| `/about` | `AboutPage` | closed loop (verdict, bundle, report, legal, audit) + guardrails |

Every page shares `Layout`: MOCK DATA banner, REPLAY MODE / "LLM disabled (extractive mode)" banners from `/health`,
offline notice, and a footer `HealthBadge` (resolved model ids, tool status summary, local model status).

## Guardrails in the UI

* **G8**: `lib/verdict.ts` maps (verdict, evidence_state, abstain_reason) to a visual. `no_evidence` (or reason
  `NO_EVIDENCE`) is always neutral grey, dashed, search icon, "No evidence found — this is NOT the same as false",
  with no confidence meter, even if the label says FALSE (then it also warns about G1). Refuted FALSE is a solid red header
  with "Refuted by N independent sources". Each of the 5 states differs in icon, border, fill and words, not only colour.
* **G5**: reports need *Prepare report*, then review of the exact `preview_text`, plan, bundle contents and card, then an
  "I confirm" checkbox + button per platform. Legal drafts need all four `LegalAcknowledgements`. There is no send button.
* Links in evidence/provenance are only the `url` fields returned by the API. Mock URLs are all `https://example.org/mock/...`.
* Every `AbstainReason` has plain-language copy (`lib/abstain.ts`, typed as `Record<AbstainReason, ...>`).

## Live trace

`hooks/useJob.ts`: `GET /verify/{id}`; if not finished, subscribe to `GET /verify/{id}/events` (EventSource). Unnamed
`data:` messages are the primary channel; named events `node, status, job, progress, tool, log, done, complete,
completed, verdict, end, error` are also accepted (payload `type` wins over the event name). Node events
`{"type":"node","node","status":"start|end|error","duration_ms"}` drive the Gantt; anything else goes into the event
log and, if it carries `status` / `status_detail` / `queue_position`, updates the status panel. On stream error or close
it switches to polling every 1.5 s (and polls every 4 s while SSE is up). A finished run always renders the server's
`JobView.trace` (authoritative timestamps); a running one merges the partial server trace with live events, so a
reconnect that replays history in a burst still lays out correctly. Node aliases (`evidence` -> `evidence_branch` etc.)
are tolerated; unknown nodes become extra rows.

## Measured

* Bundle (production): main JS 268 kB (86.6 kB gzip), CSS 36 kB (7.9 kB gzip); routes split: Job 77 kB, Audit 21 kB (incl. QR),
  Share 3.6 kB, About 4.1 kB. Service worker precaches 25 files (425 KiB).
* Tests: 8 files, 104 tests (verdict mapping incl. G8, abstain copy for every enum, platform detection, share parsing,
  trace model, placeholders, server-side rendering of VerdictCard/EvidenceList/ForensicPanel, fixture invariants).
* No horizontal scroll at 360 px on any route (checked with Chrome mobile emulation, all scenarios).
* WCAG contrast of every text token pair: min 6.57:1 (light), 5.02:1 (dark).
