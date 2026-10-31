# Reasoning: evidence, fusion, debate, calibration

Owner: reasoning. Requirements: `docs/SPEC.md` §3, §5, G1-G9, E4/E5/E9. Interfaces: `docs/BUILD_SPEC.md` §5 and §7;
decisions D-13 (dual-key verifier), D-15 (keyless first) and D-16 (the LLM is never a source).

```python
from veriloop.services.evidence import gather_evidence, followup_search, prewarm
async def gather_evidence(extraction, resolved, locale, deps, *, job_id=None) -> BranchResult      # never raises
async def followup_search(query, deps, *, start_index=1) -> list[EvidenceItem]                     # ids D{n}
from veriloop.services.fusion import fuse, decide, extractive_rationale_for                         # pure
def fuse(extraction, evidence, forensic, provenance, settings, *, config=None, calibration=None) -> FusionResult
def decide(extraction, fusion, debate, forensic, provenance, all_items, settings, *, llm_enabled,
           degraded_tools=(), config=None) -> Verdict
from veriloop.services.debate import run_debate, verify, extractive_judge, prewarm
async def run_debate(extraction, fusion, items, forensic, provenance, deps) -> DebateResult          # never raises
```

```bash
PY=/c/Users/lokar/.veriloop/venv/Scripts/python.exe; cd backend
$PY -m pytest tests/services/evidence tests/services/fusion tests/services/debate -q     # unit, no network
$PY -m pytest tests/services/debate/test_smoke_live.py -m "network and ml" -s            # live smoke (a) + (d)
$PY scripts/sync_claimreview.py [--refresh] [--embed-cap N] [--skip-upsert]              # ClaimReview index
$PY scripts/calibrate.py [--refresh] [--no-write]                                        # Platt fit + metrics
```

## 1. Evidence branch (`services/evidence`)

Every external call goes through `deps.tools.call` (breakers, chaos, cache, cassettes). Tool fns return
raw text/JSON and parsing happens afterwards, so live, cached and replayed runs are identical.

1. **Plan** (`text.plan_queries`): keyword query, fact-check variant (`<entities> fact check`), and for
   non-English claims (E9) the original-language query and locale next to the English one (`text_en`).
   Indian-government claims (cabinet, ministry, DA, pay commission ...) also query PIB.
2. **Sources in parallel**, 5 s deadline for the required ones (each search 4 s, no retry inside the
   deadline; a failed search is re-run on its secondary tool instead):
   - ClaimReview feed index (`claimreview_feed`): FTS top-50 on content words, plus HNSW neighbours,
     then an embedding rerank (missing vectors are embedded and stored on the fly). Keep similarity >= 0.6.
   - Google Fact Check API (`factcheck`), only when the key is set (else the note `unconfigured`).
   - Google News RSS (claim and fact-check variants, English and native language), plus wire via `site:`
     filters (D-09), Bing News RSS, PIB RSS (government claims only; no dates), Wikipedia, and Wikidata
     (wbsearchentities + SPARQL P39/P570) for named persons.
   - Serper (`serper`), only when keyed.
   - GDELT is **optional**. It is never awaited past the required sources (10-20 s, frequent 429s, HTTP-200
     text errors); it finishes in the background and warms the cache. Absent means absent.
   - A failed primary search is re-run on `deps.tools.secondary_for(tool)`. When the secondary already
     ran the same query it is a cache hit.
3. **Rank**: dedupe by URL and by (publisher, headline). Mark fact-checks (`data/lists/factcheckers.json`;
   mixed outlets such as The Quint and India Today need a fact-check path or title). Apply a lexical
   prefilter to 48 candidates, then embed and rank by relevance. Keep up to 24, with ClaimReview matches
   and Wikidata pinned.
4. **Enrich in parallel** (7 s deadline):
   - Stance: batched LLM calls of 8 items, effort none, each capped at 5 s. A batch that fails or times
     out falls back to local NLI, then to the lexicon, for its items only. Every support/refute needs an
     EXACT quote that `locate_quote` maps back to a span of the snippet; otherwise the stance becomes
     neutral. Hedge cues (`may`, `could`, `expected`, `will it be`, `?`, Hindi `हो सकता है` ...) cap
     stance at neutral unless the item is a fact-check carrying a verdict word. Relayed one-party claims
     ("Iran says it shot down ...") are neutral by prompt.
   - Google News link decoding (2 calls per link, 3 s, cached for 7 days).
   - Keyless ClaimReview recovery: up to 4 fact-check pages are fetched in parallel (`page_fetch`, 3 s each). schema.org
     ClaimReview JSON-LD is parsed when present; otherwise verdict words in the headline are used
     (English, Hindi, Telugu, Bengali, Tamil). The method shows in `rating_text`: headline-detected
     ratings end with `(detected in headline)` and get reliability 0.8.
5. **Independence groups** (G4): a registrable domain (tldextract, bundled suffix list, no download) is
   mapped through `syndication.json` ownership groups. All central-government sources form the single
   group `govt_india`. Dateline wire tokens such as `(PTI)` collapse to the wire. `X on MSN` is attributed
   to X; a nameless aggregator page is not counted. Near-duplicate **snippets** collapse when cosine > 0.92
   AND token Jaccard >= 0.6. Only items with a real body snippet take part: Google News items carry only a
   headline, and every outlet reporting one announcement writes nearly the same headline, so comparing
   headlines merged PIB's release with The Hindu's report of it. Government is never merged with
   non-government.
6. **URL validator**: an item survives only if its URL was returned by a tool in this job. Ids are E1..En,
   stable within the job. Items are persisted with their embeddings when a job id is known. The status is
   `ok`, `degraded` (a key search tool failed) or `insufficient` (nothing usable).

`followup_search(query)` runs Google News, Bing and the feed, keeps the top 5 by relevance as D-items
(stance unknown), and passes them through the same URL validator.

### Similarity scales (measured 2026-10-31, `intfloat/multilingual-e5-small`)

e5 cosines are compressed into a narrow band, and the band differs with the `query:` / `passage:` prefix.
The claim DA-hike sentence against probes:

| pair | passage-passage | query-passage |
|---|---|---|
| same claim, reworded | 0.962 | 0.874 |
| paraphrased headline | 0.943 | 0.864 |
| same claim, Hindi vs English | 0.865 | 0.912 |
| same topic, different event (state DA) | 0.888 | 0.819 |
| same topic, speculation | 0.876 | 0.840 |
| unrelated (cricket / cyclone) | 0.778-0.792 | 0.695-0.714 |

The earlier code compared a query-prefixed claim with passage-prefixed reviews on one scale. A reworded
review of the same claim (0.87) then rescaled to 0.47, below the 0.6 match threshold, so the feed fast
path rarely fired. Now `claim_match = max(rescale_pp(pp), rescale_qp(qp))`:

- passage-passage: 0.80 maps to 0, 0.95 maps to 1.
- query-passage: 0.78 maps to 0, 0.90 maps to 1.

Paraphrases and cross-lingual matches pass; the different-event (0.59) and speculation (0.51) probes do
not. Relevance uses the query-passage scale (0.75 maps to 0, 0.90 maps to 1). This is a fit on a small
probe set, so the stance step remains the second gate: a review of a different claim is `neutral` and
never counts.

## 2. Fusion (`services/fusion`): pure, deterministic, `config/weights.yaml` + `config/calibration.json`

| signal | s in [-1, 1] | w | r (reliability) |
|---|---|---|---|
| claimreview | mean over independent groups of (signed rating x similarity), similarity >= 0.6 | 0.35 | IFCN status (1.0 verified ... 0.7 not listed; PIB Fact Check 0.85) x 0.8 if headline-detected |
| news_stance | (support_groups - refute_groups) / max(1, stance_groups) | 0.25 | mean stance confidence (floor 0.5) |
| gdelt | min(1, groups / 5), corroboration only; no coverage means absent | 0.10 | 0.5 |
| wikidata | +1 consistent / -1 contradicted (office held P39 with dates, death P570) | 0.10 | 0.5 / 1.0 |
| provenance | known-fake -1; out-of-context -0.7 x score | 0.20 | 1.0 |

`T = sum(w*s*r) / sum(w)` over **present** signals only (E4, G8). `p_true = sigmoid(a*T + b)` (Platt).
`M = max(conclusive forensic P(fake), 0.9 known-fake manipulated, 0.9 manipulation ClaimReview at
similarity >= 0.6)`. Group counts use net stance per independent group. Aggregator groups and Wikidata
never count.

`decide` follows BUILD_SPEC §7 in order:

1. NOT_CHECKABLE.
2. E5 CONFLICT: T >= 0.5 with >= 2 supporting groups but forensics MANIPULATED; both sides are cited.
3. MANIPULATED: a manipulation finding AND M >= 0.8 AND >= 2 documenting groups; forensics alone never
   suffices.
4. TRUE / FALSE: calibrated p >= ABSTAIN_THRESHOLD, >= 2 groups, and twice the opposing side; otherwise
   CONFLICT.
5. The most specific abstain reason: NO_EVIDENCE, TOOL_OUTAGE (>= 2 key tools failed and nothing found),
   LANGUAGE_UNSUPPORTED, FORENSIC_INCONCLUSIVE, SINGLE_SOURCE, LOW_CONFIDENCE.
6. Debate: judge != candidate gives JUDGE_DISAGREEMENT; verifier not ok gives UNGROUNDED_RATIONALE
   (failing sentences listed); second-judge disagreement multiplies confidence by 0.7.
7. G1/G4/D5 gate: >= 2 independent groups among the cited ids on the label's side, and every cited id
   exists.
8. Report eligible for FALSE / MANIPULATED. Legal eligible for MANIPULATED with an identifiable subject;
   a private or unknown subject also needs forensic MANIPULATED and >= 2 groups (G9).

`evidence_state` separates `no_evidence` (nothing relevant) from `refutes` (G8). A tool outage is
TOOL_OUTAGE, never a refutation. `fast_path` is set when >= 2 independent ClaimReviews at similarity
>= 0.85 agree in sign.

## 3. Debate (`services/debate`): Tool-MAD + dual-key verifier

- **Proponent** (news / wire / GDELT / Bing / Wikipedia / Wikidata items) and **opponent**
  (ClaimReview and fact-check items, provenance P-items, forensic context) run in parallel at effort none.
  Each sees only its own items and may request ONE `followup_search`. Follow-up results are renumbered
  into contiguous D-ids, and URLs the job already has are dropped. Points whose ids do not exist are
  removed.
- **Judge** (effort low) sees the full table, both arguments, the fusion signals and the non-citable
  forensic/provenance context. It emits TRUE / FALSE / MANIPULATED / UNVERIFIABLE plus 2-4 cited
  sentences. Ids are normalised but never filtered, so an invented id reaches the verifier and fails.
- **Fast path**: the proponent and opponent are skipped; only the judge and verifier run.
- **Follow-up gating**: an agent's follow-up search is granted only when its own side has fewer than 2
  independent groups (budget 3.5 s). A proponent with six supporting groups gains nothing decisive from
  five more headlines, and each follow-up delays the judge.
- **Verifier** (D-13): a sentence passes only if all three keys pass.
  - (1) Deterministic: >= 1 id, every cited and inline id exists in the job (E/P/D), and every URL in the
    text is a job URL.
  - (2) LLM key: ONE batched support check (effort low) over the cited items' tool data. That data
    includes each item's **header** (source name, domain, publication date), so "The Hindu reports …"
    can be confirmed. For an extractive judge, this key is an exact-substring check of the quoted span.
  - (3) Local key (no LLM): ONE NLI pair per sentence, run in parallel with (2). The premise is the cited
    items joined, at most 450 chars, with question-form fact-check headlines ("X? No, Y") rewritten to
    "It is not true that X. Y". The hypothesis is the sentence minus ids and any leading attribution.
    - Pass if entailment >= NLI_ENTAIL_MIN.
    - Otherwise pass only if there is no contradiction (< 0.5) AND the cited text covers the sentence
      deterministically: every number appears in it, and >= 75 % of the content words do.
    - NLI is capped at 4 s. On timeout the coverage check alone is the local key (`methods` shows
      `nli_timeout`, `lexical`).
  - If the LLM verifier fails, its sentences fail (fail closed). Any failing sentence rejects the
    verdict (UNGROUNDED_RATIONALE).
- **Why the local key changed** (2026-10-31 live runs). Every rationale of cases (a) and (b) was
  rejected. The LLM verifier never saw the source name or date, so "The Financial Express reports …"
  was unverifiable. NLI against headline-length premises failed correct sentences: 0.48 for a
  question-form headline, and ~0 when the judge copied the claim's date. Measured NLI on these shapes:

  | sentence shape | entailment | contradiction |
  |---|---|---|
  | grounded paraphrase / summary | 0.96-0.996 | <= 0.002 |
  | claim date added (not in cited text) | 0.000 | 0.002 (neutral) |
  | number changed (3% -> 4%) / actor changed | 0.000 | 0.97-0.999 |
  | added fact ("also doubled HRA") | 0.001 | 0.003 (neutral, caught by coverage) |

  Regression set: `tests/services/debate/test_verifier_regression.py`, using the demo sentences.
  - Default run (no ML): NLI scripted to the measured scores.
  - `-m ml`: the real model. All grounded sentences pass; changed-number, added-fact,
    date-not-in-evidence and polarity-flip all fail, even when the LLM key says "supported".
- **Second judge** (`deps.llm2`, gpt-5.6-luna) runs the same prompt in parallel with the verifier; its
  agreement is recorded.
- **LLM disabled, or the judge call failed**: extractive judge. The label is the fusion candidate and the
  rationale is `Source: "exact quote"` sentences with ids. `judge.extractive=True`, labelled in the UI.

D-16 in practice:

- Every prompt forbids outside knowledge.
- No LLM schema has a URL field.
- LLM text never becomes an EvidenceItem. Items come only from tool responses; LLM stance quotes must
  be substrings of tool snippets.
- Any sentence without valid citations fails the verifier and is dropped from the verdict.

## 4. ClaimReview index (`scripts/sync_claimreview.py`, run 2026-10-31)

| what | value |
|---|---|
| feed file | `D:/veriloop/datasets/claimreview/data.json`, 200.9 MB, snapshot of 2026-10-31 |
| parse + upsert (streaming `raw_decode`, COPY + merge) | 94,063 elements (99 `item: null`), 99,698 reviews parsed, **99,443 unique rows** upserted in **269.7 s** |
| embedded rows (multilingual-e5, `passage:`) | **42,471** (15,631 added in this run, ~24 min at 10-21 rows/s on a CPU shared with other jobs; stopped early, see below) |
| priority order | India-related publishers or Indic script + reviewed within 3 years, then India older, then other recent, then the rest |
| newest review date | 2026-10-30 |
| FTS latency (top-50) | full-sentence OR-query 0.2-1.0 s; top-10 keywords 30-215 ms (now used) |

The embedding pass was stopped at 42,471 rows because it starved the CPU while latency was being
measured. That is safe: the query side embeds any missing FTS candidate on the fly and stores it, and
`--skip-upsert --embed-cap N` resumes the backfill.

## 5. Calibration (`scripts/calibrate.py`, run 2026-10-31)

**Setup.** Calibration is extraction-free. Each of the 35 checkable rows of `data/eval/claims.jsonl`
becomes a one-claim extraction, runs through `gather_evidence` (real network, LLM stance) and then
`fuse` + `decide` without the debate. The 5 NOT_CHECKABLE rows test the extraction gate and are
excluded. Evidence is cached under `D:/veriloop/datasets/eval_cache/`.

**Fit.** Platt scaling on the 21 TRUE/FALSE train rows (smoothed targets, L2 0.01) gives
`p_true = sigmoid(3.745 * T + 0.407)`, version `platt-2026-10-31-n21`.

- Gather: 225 s wall for 35 claims at concurrency 2, median 10.4 s per claim.
- LLM: 68 stance calls, INR 1.46.

| metric | test (n = 11) | train (n = 24) |
|---|---|---|
| decisive verdicts emitted (coverage) | 5 (0.45) | 18 (0.75) |
| selective precision (accuracy on emitted verdicts) | **1.00** (5/5) | **1.00** (18/18) |
| abstain rate | 0.55 | 0.25 |
| accuracy counting correct abstentions on UNVERIFIABLE rows | 0.64 | 0.88 |
| Brier, P(true) on TRUE/FALSE rows | 0.044 (n = 9; uncalibrated prior 0.060) | 0.013 (n = 21) |
| ECE, 5 bins | 0.19 (prior 0.20) | 0.10 |
| abstain reasons | SINGLE_SOURCE 5, LOW_CONFIDENCE 1 | SINGLE_SOURCE 4, LOW_CONFIDENCE 2 |

**Scoring rule.** Rows whose notes describe manipulated media count MANIPULATED as correct, i.e. AI-generated,
digitally altered and deepfake rows (SPEC: MANIPULATED = synthetic/deepfake findings). Under the
literal "Deepfake"-only reading of `data/eval/README.md`, train precision is 0.78; test is unchanged at 1.00.

**ABSTAIN_THRESHOLD.** Precision is 1.0 at every threshold from 0.50 to 0.95. Coverage is flat from 0.50
to 0.75 and falls above that (train 0.71 at 0.80, 0.13 at 0.95). The threshold is therefore not the
binding constraint; the 2-group rule is. The rule "best train coverage at precision >= 0.90, ties to the
highest threshold" picks **0.75**. The current default **0.70** gives identical measured results, so
either is supported; 0.50 would also score the same but leaves no margin.

**Honest reading.**

- Every abstention is SINGLE_SOURCE or LOW_CONFIDENCE.
  - The Factly-only FALSE rows (F-010, F-014, F-015) abstain as the eval README predicts.
  - 4 TRUE rows (T-001, T-005, T-023, T-027) abstain. Retrieval found the story, but only one publisher
    group got a decisive stance. The stance LLM is conservative, and "1 group" is a hard abstain.
  - T-001 is the demo (a) fact in different wording. The smoke test (§6) with the demo wording got 3
    supporting groups and TRUE, so the outcome is sensitive to phrasing and to live coverage.
- No decisive verdict was wrong on either split.
- The first full run (before the similarity, attribution and contention fixes) had train precision 0.75
  (U-005 emitted TRUE from "Iran says it shot down ..." reports; AI-generated rows were mis-scored) and
  several 45-60 s rows from a lazy NLI load mid-run. Those causes were fixed (§1, §3) and the run repeated.
- n is tiny: 12 test rows, 9 of them binary. Treat these as smoke-level numbers, not accuracy claims (D6).

The live smoke in §6 ran on the first-run fit (a = 3.354, b = 0.263). Under the final fit, case (a)'s
T = +0.32 maps to p_true 0.83 instead of 0.79.

## 6. Live smoke: demo cases (a) and (d), real LLM, cold tool caches

`tests/services/debate/test_smoke_live.py` (2026-10-31; laptop shared with two other engineers' ML jobs,
~0.9 GB of 16 GB RAM free):

| | case (a) DA hike | case (d) fitment factor 2.86 |
|---|---|---|
| verdict | **TRUE**, confidence 0.79 | **UNVERIFIABLE** / SINGLE_SOURCE |
| evidence | 24 items, 15 publisher groups, 8 government-source items (PIB) | 18 items, 11 groups, 4 fact-check items |
| fusion | T = +0.32, p_true 0.79, 3 supporting / 0 refuting groups | T = +0.10, 1 supporting group (one unhedged headline) |
| debate | judge TRUE, verifier ok (3/3 sentences), second judge (gpt-5.6-luna) agrees; opponent follow-up D1-D3 | judge UNVERIFIABLE, second judge agrees; 3 meta sentences rejected (no effect on an abstain) |
| cited / independent | E1, E2, E7, E11, E12 / 5 groups | E5 / 1 |
| latency | evidence 8.5 s (sources 2.1, rank 1.3, enrich 4.9) + debate 10.7 s = **19.2 s** | evidence 6.3 s + debate 11.7 s = **17.9 s** |
| LLM calls / cost | 7 calls, INR 0.24 | 7 calls, INR 0.23 |

Model prewarm (embedder + NLI load + warm-up) took 46.6 s and runs at startup, outside the job.

**Before / after the latency work**

| stage | lead's live runs | first live diagnostic (same laptop) | after |
|---|---|---|---|
| evidence | 27-34 s | 12.3-13.3 s | 6.3-8.5 s (12-18 s under heavier contention) |
| debate: agents | ? | 3.0 s | 1.7-2.5 s (parallel, effort none) |
| debate: follow-up | ? | 5.0 s | 0 s (side already strong) or <= 3.5 s |
| debate: judge | ? | 2.3-4.6 s | 1.7-2.2 s |
| debate: verifier ∥ second judge | ? | 28-30 s (NLI on 1,500-char premises, run after the LLM key) | 4.0-4.8 s (one LLM call ∥ NLI capped 4 s ∥ second judge) |
| debate total | 67-76 s | 33-40 s | 6.4 s (fast path) - 11.7 s |
| end to end | 98-113 s | 45-54 s | 18-25 s |

The < 8 s text target is **not met**, and cannot be with this design on this provider. Four sequential
LLM round trips are required (stance -> proponent ∥ opponent -> judge -> verifier ∥ second judge), and
they measure 1.7-5 s each here (effort none 1-3.6 s, low 1.7-4.8 s). Options for the lead:

- Treat strong text consensus (>= 3 independent agreeing groups) like the fast path, skipping
  proponent/opponent: saves 2-6 s.
- Record the second judge after the verdict instead of on the critical path: saves ~1-2 s.
- Run the verifier at effort none: saves ~1-1.5 s.

Each one trades away a spec requirement, so none is done unilaterally.

## 7. Limitations and known gaps

- **Latency**: 18-25 s end to end for a text claim on this laptop, against a < 8 s target. See §6 for
  why and for the options.
- **Strict verifier**: one ungrounded sentence rejects the verdict (SPEC §5). In one live run the judge
  wrote "effective January 1, 2026"; NLI vetoed it (contradiction 0.91) and the TRUE verdict became
  UNGROUNDED_RATIONALE. That is the guardrail working, but it costs a correct verdict. A single judge
  repair round (rewrite only the failing sentences, re-verify everything) would recover these cases;
  it is not implemented, pending a decision on the audit trail.
- **When NLI times out** (heavy contention), the local key is word and number coverage only. It cannot
  see negation, so a polarity flip is then caught only by the LLM key.
- **Stance**: hedges are capped by lexicon and prompt. An unhedged speculative headline ("Employees To
  Get 2.86 Fitment Factor") still counts as support. The 2-group rule absorbs one such group, which is
  case (d). Relayed one-party claims ("Iran says …") are neutral by prompt only.
- **Similarity rescaling** (§1) was fitted on a small probe set. The stance step remains the second gate.
- **ClaimReview index**: 42,471 of 99,443 rows embedded (on-the-fly embedding covers the rest). Recent
  India coverage in the feed is ~99 % Factly, so most FALSE claims have one feed group; the second group
  must come from news RSS fact-check articles.
- **Debate D-items** have unknown stance (no extra LLM call), so they can be cited but never count
  toward the 2-group gate.
- **GDELT** rarely contributes: it is never awaited, often rate-limited or slow, and lags ~6 days.
- **PIB RSS** items have no date. The Google News link decoder is undocumented and may break; on failure
  the encoded link is kept.
- **Bing and Google News RSS terms** restrict redistribution: these are demo-use snippets with links back.
- **Calibration** is extraction-free and text-only (no media, no translation of non-English rows), on 12
  test rows. Confidence intervals are wide.
