# Media: ingestion, extraction, forensics, provenance

Owner: media engineer. Requirements are in `docs/SPEC.md` §3–4 and edge cases E1, E2, E7, E8 and E9. Interfaces are in `docs/BUILD_SPEC.md` §5.
Every number below was measured on this laptop (CPU only) on 2026-10-31. Nothing here is a model-card claim unless it says so.

```bash
PY=/c/Users/lokar/.veriloop/venv/Scripts/python.exe; cd backend
$PY -m pytest tests/services/ingestion tests/services/extraction tests/services/forensic tests/services/provenance -q
$PY -m pytest tests/services/ingestion/test_smoke_video.py -m "network and ml" -s     # case (b) video path
$PY -m pytest tests/services/forensic/test_smoke_models.py tests/services/provenance/test_smoke_match.py -m ml -s
$PY scripts/seed_known_fakes.py       # known-fake index (idempotent)
$PY scripts/fetch_demo_media.py       # demo clips for cases b and c -> VERILOOP_HOME/demo, seeded as known fakes
$PY scripts/eval_forensics.py         # labelled set -> data/eval/forensics/results.json + forensic/weights.json
```

## Public interface

```python
from veriloop.services.ingestion import resolve_input, prewarm      # (request, upload_path, job_id, deps) -> ResolvedInput
from veriloop.services.extraction import extract_claims, prewarm, visual_queries, status
from veriloop.services.forensic import run_forensics, prewarm, status    # (resolved, extraction, deps) -> ForensicResult
from veriloop.services.provenance import run_provenance, prewarm, status # (job_id, resolved, extraction, deps) -> ProvenanceResult
```
* None of the four functions raises to the orchestrator. A tool failure becomes a `ToolError` plus a branch status. An unexpected bug degrades the result: text-only input, heuristic claims, or `FAILED`/`unavailable` for a branch.
* `prewarm(deps)` loads the local models once. Extraction loads Whisper small and base plus EasyOCR. Forensic loads YuNet and the 3 HF detectors. Provenance loads CLIP. `status()` returns the model states for `/health.ml`.
* `visual_queries(extraction)` returns the search queries the LLM wrote from keyframes. These are the D-15 keyless substitute for reverse-image search. They live in `extraction.notes` with the prefix `visual_query: ` and come with the note "...they are search queries, not evidence". The evidence branch may search with them but must never cite them.
* External calls all go through `deps.tools.call` using registered tools:
  * `oembed`, `page_fetch` (pages, images, short-link expansion, the X syndication JSON, the YuNet file), `ytdlp` and `playwright_fetch`;
  * `vision_web`, `factcheck` (imageSearch), `sightengine` and `hf_inference`. These are keyed: they report `unconfigured` and add a note while their key is missing.

  HF, open_clip, faster-whisper and EasyOCR weights come through each library's own downloader. The cache dirs are always `settings.models_dir/*` on D:.

## Ingestion (`services/ingestion`)
* **URL**: normalise (tracking params dropped; YouTube to `watch?v=`, X mirrors to `x.com`), expand short links (HEAD, then GET, through `page_fetch`), then detect the platform.
* **Metadata, cheapest first**: oEmbed (YouTube / X / TikTok) runs in parallel with yt-dlp. X posts with no video then use the public syndication JSON. Next come page `og:`/`twitter:`/JSON-LD meta (blocked and login walls are detected even on HTTP 200). Headless Chromium is the last resort (E1), returning title, meta, visible text and a screenshot.
* **Media**:
  * yt-dlp downloads with size caps and `MAX_VIDEO_S`. It fetches metadata and media in one extraction, with a cancel flag on tool timeout.
  * ffprobe, then frames at `FRAME_FPS`, capped at `FRAME_CAP` and spread evenly across long videos. Samples sit at interval centres (E8).
  * A video under 120 s is decoded in one pass; longer ones use parallel seeks.
  * Audio is extracted in parallel: mono, 16 kHz WAV, capped at `AUDIO_CAP_S`.
  * Every image or frame gets a sha256, a 64-bit pHash and its size, and is persisted to `media_assets`.
* **Labels**:
  * `Media inaccessible (login-walled) — analyzed caption only` (E2)
  * `Visual forensics unavailable — analyzed caption only` (E1)
  * `Only a platform thumbnail was accessible — visual forensics limited`
  * `Sampled 16 frames of 412 s video` (E8, added whenever the cap applied)
  * a partial-download note when a video exceeds `MAX_VIDEO_S`

## Extraction (`services/extraction`)
* **ASR**: faster-whisper with ASR_MODEL `small` (int8, CPU, VAD, greedy). It switches to `base` when the predicted run exceeds 6 s; the prediction uses the measured real-time factor, updated every run. Transcript language comes from Whisper.
* **OCR**: EasyOCR en+hi on up to 4 keyframes, chosen to be pHash-distinct and downscaled to 960 px.
* **Visual query expansion**: up to 2 keyframes go to `deps.llm` (effort `none`), all in parallel with ASR and OCR.
* **Claims**: one structured LLM call (`extract_claims`, effort `none`) over title, caption, transcript and OCR text. It returns:
  * atomic claims with an English `text_en` for search (E9)
  * the primary claim
  * entities and claim type
  * check-worthiness
  * subject info (identifiable individual; public or private; name)

  D-16 guard: a claim the LLM returns that is not lexically grounded in the content is dropped and noted. Language comes from the LLM, with langdetect as the fallback (a Devanagari short-circuit gives `hi`).
* **LLM disabled or failing**: extractive heuristics instead. Sentence split, then worthiness from event verbs, numbers, dates, entities and "video shows". Recipe, greeting, promo, opinion and question content scores low (E3). A non-English claim gets the note "no translation available" (E9).
* **ASR vs OCR mismatch**: e5 cosine (`deps.embedder`) mapped to 0..1. It falls back to lexical overlap only when both texts are in the same language.

## Forensics (`services/forensic`)
**Routing**: YuNet face detection runs first (`face_detection_yunet_2023mar.onnx`, fetched once through `page_fetch` into `models_dir/opencv`). OpenCV 5.0 in this venv has no Haar cascades, so without YuNet faces are not detected and the result says so. Then:
* Faces of at least 64 px are cropped with a 30 % margin per side and go to the 2 face detectors.
* Frames without faces go to the AI-generated-image detector.
* The eval measured that detector at AUC 0.888 on face images too (`full_model_on_face_frames: true` in `weights.json`), so it also votes on face frames.

Per-model score = mean of the top 3 frame scores. Every per-frame score is kept: it is persisted to `forensic_scores` and summarised in the notes.

**Models.** HF commit shas below are verified against `huggingface.co/api/models/<id>`; training data is from the model cards.

| role | model id | revision | card says |
|---|---|---|---|
| face | `dima806/deepfake_vs_real_image_detection` | `29e4cf9efc543845610045f6ba7e88e5cf9d9301` | ViT-base fine-tuned on a Kaggle real/fake face set. The card warns the data is ~3 years old (concept drift). apache-2.0 |
| face | `prithivMLmods/Deep-Fake-Detector-v2-Model` | `3a99ae26f52c7ac7c3a53103b6cf3a8b617f7093` | ViT-base, labels Realism/Deepfake, card eval 56k images. apache-2.0 |
| full image | `buildborderless/CommunityForensics-DeepfakeDet-ViT` | `ac6ee457bea904a373065754107451793b56db00` | ViT-Small/384, Community Forensics (CVPR 2025), 2.7M images from 4,803 generators. Single sigmoid = P(AI-generated). Fixed weights re-uploaded 2026-07-22 and needs transformers ≥ 5.4 (we have 5.22.1). MIT |
| commercial | `sightengine/deepfake`, `sightengine/genai` | API 1.0 | Keyed (`SIGHTENGINE_API_USER/SECRET`). Weight 0.5 because unmeasured. Unconfigured now |
| remote | HF Inference for the same pinned ids | — | Used only when `HF_TOKEN` is set. Otherwise local |

**Ensemble and abstain band.**
* The ensemble is a weighted mean of P(fake) over the models that produced a score and have weight > 0. Each weight is `clip(2 × (AUC − 0.5), 0, 1)` from our own eval (`forensic/weights.json`).
* **INCONCLUSIVE** when any of these holds (SPEC §4, `FORENSIC_LOW/HIGH/DELTA` = 0.35/0.70/0.40):
  * fewer than 2 weighted models scored;
  * max − min > DELTA (strictly greater);
  * every score is in [LOW, HIGH] (inclusive);
  * the ensemble itself is in (LOW, HIGH), so a low-confidence score can never become "manipulated";
  * E7: a JPEG with estimated quality < 50 (from the quantisation table), media under 160 px, video under 240 px, or video below 0.015 bits/pixel/frame.
* Otherwise **MANIPULATED** when the ensemble ≥ HIGH, and **REAL** when it is ≤ LOW. Forensics alone never yields a MANIPULATED verdict: fusion also requires 2 manipulation groups (BUILD_SPEC §7).

**Metadata** is a bonus signal and never votes:
* EXIF presence, editing-software names and suspected stripping on uploads;
* JPEG re-encode quality;
* FFmpeg/encoder container hints;
* C2PA via c2pa-python (signer, issuer, actions).

The sentence **"Absence of C2PA Content Credentials is not evidence of manipulation."** is always present, in `metadata.c2pa_note` and in `notes`, on every path including skipped and failed.

### Measured forensic numbers (`scripts/eval_forensics.py`, `data/eval/forensics/results.json`)
**Set**: `OpenRL/DeepFakeFace` @ `eb2a54fb…` (card license `openrail`; the README body says apache-2.0; the real photos come from IMDB-WIKI).
* 60 real (wiki) and 60 fake: 20 InsightFace face swaps, 20 SD-inpainting, 20 SD-1.5 text2img.
* Sampled with a fixed seed. Only those members were fetched, by HTTP range reads of the remote zips.
* Stored under `D:/veriloop/datasets/forensics`. Sample ids and sha256 are in `data/eval/forensics/manifest.csv`.
* YuNet found a usable face in 99/120 images.

| model | n | AUC orig | AUC q60 | AUC q30 | acc@0.5 orig | AUC real vs swap / inpaint / text2img |
|---|---|---|---|---|---|---|
| dima806 (face crops) | 99 | 0.543 | 0.519 | 0.508 | 0.556 | 0.532 / 0.580 / 0.517 |
| prithivMLmods v2 (face crops) | 99 | 0.540 | 0.582 | 0.580 | 0.525 | 0.510 / 0.623 / 0.487 |
| CommunityForensics (whole image) | 120 | **0.874** | 0.839 | 0.760 | 0.783 | 0.635 / 0.990 / 0.998 |

Weights from these AUCs: 0.085 / 0.080 / 0.748.

**Ensemble with the production routing and band.** Weights are 2-fold cross-fitted: each half is scored with weights measured on the other half.

| variant | ensemble AUC | abstain rate | coverage | accuracy on decided | real called MANIPULATED | fake called REAL |
|---|---|---|---|---|---|---|
| original | 0.808 | 70 % | 30 % (36/120) | 77.8 % | **0** | 8 |
| JPEG q60 | 0.747 | 67.5 % | 32.5 % | 71.8 % | 0 | 11 |
| JPEG q30 (E7) | 0.580 | **100 %** (quality gate flagged 120/120) | 0 % | n/a | 0 | 0 |

Why it abstains:
* 58 images: the near-chance face models disagree with CommunityForensics by more than DELTA.
* 21 images: no usable face, so only one weighted model scored.
* 5 images: the scores or the ensemble sit inside the band.

What this means:
* On this set the two public face-deepfake classifiers are **near chance**: AUC ≈ 0.54, so they barely generalise beyond their training data.
* The AI-generated-image detector carries the signal, strongly for diffusion faces and weakly for face swaps (AUC 0.635).
* The band does what the spec wants. It never called a real photo MANIPULATED, and it abstains on crushed JPEGs instead of guessing.
* Caveats:
  * 120 images is a small set: the AUC confidence intervals are wide, roughly ±0.07.
  * The set contains no lip-sync or voice deepfakes like demo case (c).
  * Forensics alone will therefore rarely decide a demo case, which is by design: provenance and fact-checks carry them.

Timing: scoring 120 images took 416 s on the first pass. That included the cold load of all 3 models and ran under CPU contention from parallel jobs.

## Provenance (`services/provenance`)
1. **Own index first** (cheap and local), for each distinct, informative image or frame:
   * exact sha256;
   * pHash Hamming ≤ 10 (`bit_count` in Postgres) against `known_fakes` and earlier jobs' `media_assets`;
   * CLIP `open_clip ViT-B-32 / laion2b_s34b_b79k` (512-d, L2-normalised), pgvector cosine ≥ 0.93.

   This job's CLIP vectors are stored for future runs.
2. **Keyed tools**:
   * Cloud Vision WEB_DETECTION (`vision_web`): pages with full or partial matches; the first 5 pages are dated through `page_fetch` from `article:published_time`.
   * Fact Check `claims:imageSearch` (`factcheck`), using the oEmbed thumbnail URL.

   Both report unconfigured without keys.
3. **First-seen estimate**: the earliest dated appearance across:
   * known-fake origins (the frame variants carry `[origin: url @ date]` from the yt-dlp metadata);
   * the manifest `first_seen_date`;
   * Vision pages;
   * our own earliest prior run.

   **Out-of-context score**: 0 when the gap to the claimed event is under 7 days. Otherwise `min(1, 0.5 + gap_days/120)`, multiplied by 0.7 when the claim states no date (the event is then assumed to be current).
4. **Context**: a matched domain on `data/lists/satire.json`, `state_affiliated.json` or `known_disinfo.json` adds a flag and a note naming the list and its evidence link.
5. **Citable items `P1..`**: one per fact-check URL of a matched known fake.
   * The URLs come verbatim from the seed manifest. The snippet says it is a VeriLoop index match.
   * When the claim is similar (≥ 0.6) the stance is `refute`, and `claimreview.manipulation_rating` is set only for `manipulated` entries.
   * Vision pages and image-search reviews are also items. A review gets `similarity = 0` because an image match is not a claim-text match.

**Known-fake index** (`scripts/seed_known_fakes.py`, `scripts/fetch_demo_media.py`, `provenance/seeding.py`):
* Each manifest row becomes a base row plus variants:
  * the full image;
  * a centre crop (the middle 70 %, so stamped fact-check thumbnails still match the clean original);
  * alternative image URLs named in the manifest notes;
  * every informative sampled frame of the demo clips, with ingestion's exact frame plan.
* The manifest label `ai_generated` is stored as `manipulated` because the DB and the contract have no such label. The original label is kept in the notes.
* Seeded 2026-10-31: **20/20** manifest entries downloaded, **46 rows**, all with CLIP. Demo media added **37 rows**:
  * case b: 15 informative frames plus 2 still rows;
  * case c: 16 frames plus 4 still rows.
* That makes **83 `known_fakes` rows** in the app database.
* Demo media lives in `D:/veriloop/demo/case_{b,c}/`:
  * `demo_case_b.mp4`: YouTube XeYAGU1Z6l8, "Fierce Winds Hit US Pacific Territories as Super Typhoon Bavi…", 34 s, uploaded 2026-07-06;
  * `demo_case_c.mp4`: PIB Fact Check X post 2112600705232249021, 19 s, 2026-10-20;
  * the still images, and `info.json` with the yt-dlp metadata.

## Latencies (warm models, this laptop)
SMOKE_TABLE

## Limitations
* The face-deepfake detectors are weak on our set (above). Forensics mostly abstains or leans on the AI-generated-image detector. There is no synthetic-speech classifier: audio is transcribed, not authenticated (stated in the notes).
* Instagram and Facebook media are usually login-walled. We then analyse the caption or `og:` description only, and keep any thumbnail for provenance but never for forensics.
* pHash and CLIP match re-uploads and crops of indexed media. They do not find the *original* of new media; that needs `GOOGLE_VISION_API_KEY` (Cloud Vision) or the LLM visual queries that feed the evidence search.
* First-seen dates for known fakes are the manifest's earliest fact-check date unless a frame variant carries an origin date. Without keys there is no web-wide earliest copy.
* EasyOCR and Whisper weights (≈ 650 MB) download on first prewarm into `models_dir`. A run without them degrades to caption-only extraction, with notes.
