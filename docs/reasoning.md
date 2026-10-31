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
2. **Sources in parallel**, 6 s deadline for the required ones:
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
   prefilter to 60 candidates, then embed and rank by relevance. Keep up to 24, with ClaimReview matches
   and Wikidata pinned.
4. **Enrich in parallel** (9 s deadline):
   - Stance: batched LLM calls of 8 items, effort none, each capped at 5.5 s. A batch that fails or times
     out falls back to local NLI, then to the lexicon, for its items only. Every support/refute needs an
     EXACT quote that `locate_quote` maps back to a span of the snippet; otherwise the stance becomes
     neutral. Hedge cues (`may`, `could`, `expected`, `will it be`, `?`, Hindi `हो सकता है` ...) cap
     stance at neutral unless the item is a fact-check carrying a verdict word. Relayed one-party claims
     ("Iran says it shot down ...") are neutral by prompt.
   - Google News link decoding (2 calls per link, cached for 7 days).
   - Keyless ClaimReview recovery: up to 4 fact-check pages are fetched (`page_fetch`). schema.org
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
- **Verifier** (D-13): a sentence passes only if all three keys pass.
  - (1) Deterministic: >= 1 id, every cited and inline id exists in the job (E/P/D), and every URL in the
    text is a job URL.
  - (2) LLM support check (batched, effort low; only for sentences that passed (1)). For an extractive
    judge this key is an exact-substring check of the quoted span instead.
  - (3) When NLI is available: max entailment(cited text -> sentence minus ids and leading attribution)
    >= NLI_ENTAIL_MIN.
  - If the LLM verifier fails, its sentences fail (fail closed).
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

STATS_PLACEHOLDER

## 5. Calibration (`scripts/calibrate.py`, run 2026-10-31)

CALIBRATION_PLACEHOLDER

## 6. Live smoke: demo cases (a) and (d), real LLM, cold tool caches

SMOKE_PLACEHOLDER

## 7. Limitations and known gaps

LIMITATIONS_PLACEHOLDER
