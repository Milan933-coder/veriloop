# VeriLoop eval / calibration set (`claims.jsonl`)

There are 40 labelled claims, one JSON object per line (UTF-8). All were verified on 2026-10-31. The set is used to fit
the Platt/isotonic calibration (`split=train`) and to report measured accuracy (`split=test`, SPEC D6).

## Counts

| label | train | test | total |
|---|---|---|---|
| FALSE | 11 | 4 | 15 |
| TRUE | 10 | 5 | 15 |
| NOT_CHECKABLE | 4 | 1 | 5 |
| UNVERIFIABLE | 3 | 2 | 5 |
| **all** | **28** | **12** | **40** |

- **Languages:** en 29, hi 8, te 1, bn 1, ta 1.
- **Topics:** governance, economy, elections, disaster, defence, foreign affairs, science, world, communal, and others.
- **Demo-case claims are in `test`:** F-001 (deepfake, case c), F-004 (case b), T-001 (case a) and U-007 (case d).

## Fields

| field | meaning |
|---|---|
| `id` | `F-` FALSE, `T-` TRUE, `N-` NOT_CHECKABLE, `U-` UNVERIFIABLE |
| `claim` | One atomic sentence, in `language`. |
| `label` | TRUE / FALSE / NOT_CHECKABLE / UNVERIFIABLE. Deepfake rows are labelled FALSE here, and the notes say "Deepfake". The system is expected to output MANIPULATED for those, so score MANIPULATED as correct for them. |
| `language` | ISO 639-1 |
| `topic`, `date_of_claim` | When the claim circulated or the event happened. |
| `evidence_urls` | Verified URLs that support the label. |
| `evidence_publishers` | Extra field: publisher names, in the same order as `evidence_urls`. |
| `split` | train / test. Stratified by label, roughly 70/30, assigned by hand. |
| `verified_at`, `verification` | Extra fields: date checked and how. |
| `notes` | Why the label holds; known distractors. |

## Provenance and labelling rules

**FALSE (15)**
- Taken from the Google ClaimReview data feed snapshot of 2026-10-31. Most are Factly reviews rated False or Misleading, where the claim is factually false.
- Demo claims also carry other fact-checkers found via Google News and Bing RSS: BOOM, Newschecker, The Quint, Fact Crescendo, Lighthouse Journalism, CyberPeace, NewsMobile.
- Every evidence page returned HTTP 200 to curl on 2026-10-31, with an og:image or keyword match.
- **Limitation: 10 of the 15 FALSE rows have only one fact-check group (Factly).** Under the system's 2-group rule (G4), those rows should produce UNVERIFIABLE/SINGLE_SOURCE unless retrieval finds a second group. Score them with that in mind, or report results on F-001 to F-004 separately.
- F-005 is a Hindi rendering of Factly's English `claimReviewed`. The viral Hindi caption is quoted in its notes. F-014 is Factly's Telugu `claimReviewed`, verbatim.

**TRUE (15)**
- Each has at least 2 evidence URLs from different ownership groups. Most pair a PIB press release with independent outlets, or a wire copy with independent Indian outlets.
- Two outlets running the same PTI copy count as one group.
- Every evidence URL was fetched and the stated fact keyword-matched (a data/research sub-task, 2026-10-31).
- Mostly August to October 2026, so they fall within GDELT's window. T-016 (May 2026) is the older slice.
- Hindi, Bengali and Tamil TRUE rows use Hindi or regional sources plus English corroboration.

**NOT_CHECKABLE (5)**
- Greetings, opinions and recipes (English, Hindi, Hinglish). No evidence is needed.

**UNVERIFIABLE (5)**
- Future outcomes not yet known as of 2026-10-31: 2036 Olympics host, 8th Pay Commission fitment factor, RBI December move.
- Single-party or single-source claims: an Iranian drone shoot-down claim.
- Unconfirmed figures: an Andhra Pradesh cyclone relief amount.
- Rule: a *specific* future or unconfirmed claim is UNVERIFIABLE; a *vague* prediction ("things will get better") is NOT_CHECKABLE.

## Limitations

- The set is small (40 rows, 12 in test), so accuracy confidence intervals are wide. Report n alongside every number.
- FALSE rows are dominated by one fact-checker (Factly), because the ClaimReview feed's recent India coverage is roughly 99% Factly.
- Labels are as of 2026-10-31. These UNVERIFIABLE rows resolve later and must be re-labelled after:
  - U-007: the Pay Commission report.
  - U-013: the RBI December meeting.
- Google News and GDELT results drift. Record cassettes when measuring.
- No ground truth for forensic scores. The forensic eval set lives in `data/eval/forensics/` (owned by forensics). Known-fake media for pHash is in `data/seed/known_fakes/manifest.csv`.
