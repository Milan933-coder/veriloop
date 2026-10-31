/**
 * MOCK scenarios: one per verdict state + abstain, no-evidence, degraded-tool and ungrounded cases.
 * All people, places, publishers, URLs and model ids are fictional (see builders.ts).
 */
import type { GenericJobEvent, JobView } from '../api/types'
import {
  BASE_MS,
  claim,
  debate,
  ev,
  forensic,
  fusion,
  input,
  iso,
  job,
  mockUrl,
  provenance,
  r,
  signal,
  standardTimeline,
  type Span,
} from './builders'

export type ScenarioKey =
  | 'true'
  | 'false'
  | 'manipulated'
  | 'abstain'
  | 'no_evidence'
  | 'not_checkable'
  | 'degraded'
  | 'ungrounded'

export interface Scenario {
  key: ScenarioKey
  title: string
  description: string
  timeline: Span[]
  job: JobView
  /** status_detail shown while processing between these offsets (E10) */
  processingDetail?: { from: number; to: number; detail: string }
  /** extra non-node SSE events at offsets (exercise the "unknown event" rendering) */
  extraEvents?: Array<{ at: number; event: GenericJobEvent }>
}

const day = (y: number, m: number, d: number, h = 9) => iso(Date.UTC(y, m - 1, d, h, 0, 0))

// ---- (a) TRUE --------------------------------------------------------------------------------

const trueSources = [
  ev({
    id: 'E1', source: 'Mock Wire A', group: 'mock-wire-a', slug: 'metro-line-2-opens',
    title: 'Mockpur Metro Line 2 to open to public on November 1',
    snippet: 'The state transport department said Mockpur Metro Line 2 will open to the public on November 1 after the safety commissioner cleared the 14-km stretch.',
    quote: 'Mockpur Metro Line 2 will open to the public on November 1',
    stance: 'support', method: 'wire_rss', tool: 'wire_rss', published: day(2026, 10, 29), relevance: 0.93,
  }),
  ev({
    id: 'E2', source: 'Mock Daily B', group: 'mock-daily-b', slug: 'city/metro-line-2-date',
    title: 'Metro Line 2 gets safety nod; opening on Nov 1',
    snippet: 'After the final inspection, officials confirmed that commercial operations on Line 2 begin on 1 November, with trains every eight minutes.',
    quote: 'commercial operations on Line 2 begin on 1 November',
    stance: 'support', method: 'google_news_rss', tool: 'gnews_rss', published: day(2026, 10, 30), relevance: 0.88,
  }),
  ev({
    id: 'E3', source: 'Government press release (mock)', group: 'mock-gov-press', slug: 'release/transport-2026-10-30',
    title: 'Opening of Metro Line 2',
    snippet: 'The Department of Transport announces that Metro Line 2 shall be opened for public service from 1 November 2026.',
    quote: 'shall be opened for public service from 1 November 2026',
    stance: 'support', method: 'pib_rss', tool: 'pib_rss', published: day(2026, 10, 30, 12), relevance: 0.95, gov: true,
  }),
  ev({
    id: 'E4', source: 'Mock Regional C (syndicated from Mock Wire A)', group: 'mock-wire-a', slug: 'regional-c/metro-line-2',
    snippet: 'The state transport department said Mockpur Metro Line 2 will open to the public on November 1.',
    stance: 'support', method: 'google_news_rss', tool: 'gnews_rss', published: day(2026, 10, 29, 11), relevance: 0.86,
  }),
  ev({
    id: 'E5', source: 'GDELT event record (mock)', group: 'mock-gdelt', slug: 'gdelt/metro-line-2',
    snippet: '27 articles from 19 domains mention Mockpur Metro Line 2 in the past 7 days; average tone +2.1.',
    stance: 'neutral', method: 'gdelt_doc', tool: 'gdelt', relevance: 0.55, stanceConfidence: 0.4,
  }),
]

const trueScenario: Scenario = {
  key: 'true',
  title: 'TRUE: metro opening date (text post)',
  description: 'Three independent groups support the claim; syndicated copy counted once.',
  timeline: standardTimeline({ forensic_branch: { dur: 40, status: 'skipped', detail: 'text-only input' }, provenance_branch: { dur: 35, status: 'skipped', detail: 'no media' } }),
  job: job({
    verdict: 'TRUE',
    confidence: 0.91,
    evidence_state: 'supports',
    rationale: [
      r('The state transport department announced that Metro Line 2 opens to the public on 1 November 2026.', 'E3', 'E1'),
      r('An independent newspaper report confirms commercial operations begin on 1 November.', 'E2'),
    ],
    cited_evidence_ids: ['E3', 'E1', 'E2'],
    independent_source_count: 3,
    sources: trueSources,
    forensic: forensic({ branch_status: 'skipped', status: 'unavailable', notes: ['Text-only input: no media to analyze.'] }),
    provenance: provenance({ branch_status: 'skipped', notes: ['No media to trace.'] }),
    claim: claim('Mockpur Metro Line 2 opens to the public on 1 November 2026, the state transport department announced.', {
      caption_text: 'BIG NEWS: Mockpur Metro Line 2 opens to the public on 1 November! Transport dept confirms.',
    }),
    input: input({ platform: 'x', input_kind: 'url', url: mockUrl('x-post/1001'), title: 'Post by @mock_commuter', fetch_methods: ['oembed'] }),
    fusion: fusion({
      candidate: 'TRUE', evidence_state: 'supports', truth_score: 0.82, p_true: 0.91, groups_support: 3,
      signals: [
        signal('claimreview', 0, 0.35, { present: false, detail: 'No ClaimReview match (absent, not negative)' }),
        signal('news_stance', 1, 0.25, { detail: '3 supporting groups, 0 refuting', evidence_ids: ['E1', 'E2', 'E3'] }),
        signal('gdelt', 0.7, 0.1, { detail: 'Broad corroboration across 19 domains', evidence_ids: ['E5'] }),
        signal('wikidata', 1, 0.1, { detail: 'Line 2 entity exists; operator matches' }),
        signal('provenance', 0, 0.2, { present: false, detail: 'No media' }),
      ],
    }),
    debate: debate({
      proponent: { role: 'proponent', points: [r('Official release states the opening date.', 'E3')], followup_queries: [], followup_evidence_ids: [] },
      opponent: { role: 'opponent', points: [r('No source contradicts the date.')], followup_queries: ['Metro Line 2 opening postponed'], followup_evidence_ids: [] },
      judge: { label: 'TRUE', rationale: [], provider: 'mock', model_id: 'mock/gpt-6-luna', extractive: false },
      verifier: {
        ok: true, methods: ['deterministic', 'llm', 'nli'], failing_sentences: [],
        checks: [
          { sentence_index: 0, text: 'The state transport department announced that Metro Line 2 opens to the public on 1 November 2026.', evidence_ids: ['E3', 'E1'], deterministic_ok: true, llm_supported: true, nli_entailment: 0.94, ok: true, reason: '' },
          { sentence_index: 1, text: 'An independent newspaper report confirms commercial operations begin on 1 November.', evidence_ids: ['E2'], deterministic_ok: true, llm_supported: true, nli_entailment: 0.88, ok: true, reason: '' },
        ],
      },
      second_judge: { provider: 'mock', model_id: 'mock/gpt-5.6-luna', label: 'TRUE', agrees: true, error: null },
      latency_ms: 3100,
    }),
    audit_id: 101,
  }),
}

// ---- (b) FALSE -------------------------------------------------------------------------------

const falseSources = [
  ev({
    id: 'E1', source: 'Mock Fact Check Desk', group: 'mock-factcheck-desk', slug: 'fact-check/airport-missile-video',
    title: 'Fact check: Viral "missile strike on Mockpur airport" video is video-game footage',
    snippet: 'The clip circulating as a missile strike on Mockpur airport is footage from a military simulation game uploaded in 2023. Airport operations were normal.',
    quote: 'footage from a military simulation game uploaded in 2023',
    stance: 'refute', method: 'factcheck_claimsearch', tool: 'factcheck', published: day(2026, 10, 31, 6), relevance: 0.95,
    factCheck: { publisher: 'Mock Fact Check Desk', rating_text: 'False', normalized_rating: -1, manipulation_rating: false, claim_text: 'Missile strike on Mockpur airport', claimant: 'Social media users', similarity: 0.87 },
  }),
  ev({
    id: 'E2', source: 'Government fact-check unit (mock)', group: 'mock-gov-press', slug: 'factcheck/airport-claim',
    title: 'Claim of strike on Mockpur airport is fake',
    snippet: 'A video claiming a missile strike on Mockpur airport is FAKE. No such incident has occurred. Please do not share unverified content.',
    quote: 'No such incident has occurred',
    stance: 'refute', method: 'pib_rss', tool: 'pib_rss', published: day(2026, 10, 31, 7), relevance: 0.93, gov: true,
  }),
  ev({
    id: 'E3', source: 'Mock Wire A', group: 'mock-wire-a', slug: 'airport-operations-normal',
    title: 'Mockpur airport operations normal, says airport authority',
    snippet: 'All flights operated on schedule overnight, the airport authority said, calling social media posts about an attack baseless.',
    quote: 'All flights operated on schedule overnight',
    stance: 'refute', method: 'wire_rss', tool: 'wire_rss', published: day(2026, 10, 31, 5), relevance: 0.9,
  }),
  ev({
    id: 'E4', source: 'Mock Daily B', group: 'mock-daily-b', slug: 'city/airport-rumour',
    snippet: 'Police said they were tracing accounts that shared the old clip with a false caption about an airport strike.',
    quote: 'shared the old clip with a false caption',
    stance: 'refute', method: 'google_news_rss', tool: 'gnews_rss', published: day(2026, 10, 31, 8), relevance: 0.82,
  }),
  ev({
    id: 'P1', source: 'Mock Gaming Clips', group: 'mock-gaming-clips', slug: 'videos/convoy-airbase-sim',
    title: 'Airbase defence: simulation gameplay (2023)',
    snippet: 'Gameplay recording: airbase defence mission, night, with missile effects. Uploaded May 2023.',
    stance: 'refute', method: 'vision_web_detection', tool: 'vision_web', published: day(2023, 5, 14), relevance: 0.78,
  }),
]

const falseScenario: Scenario = {
  key: 'false',
  title: 'FALSE: "missile strike" reel (caption only)',
  description: 'Login-walled reel; caption analyzed; 4 independent groups refute; first seen in 2023 as game footage.',
  timeline: standardTimeline({ forensic_branch: { dur: 900, status: 'ok', detail: 'media inaccessible' } }),
  job: job({
    verdict: 'FALSE',
    confidence: 0.88,
    evidence_state: 'refutes',
    rationale: [
      r('The viral clip is footage from a military simulation game first uploaded in 2023.', 'E1', 'P1'),
      r('The government fact-check unit states that no such incident occurred.', 'E2'),
      r('The airport authority reported that all flights operated on schedule overnight.', 'E3'),
    ],
    cited_evidence_ids: ['E1', 'E2', 'E3', 'P1'],
    independent_source_count: 4,
    sources: falseSources,
    forensic: forensic({
      branch_status: 'insufficient', status: 'unavailable',
      notes: ['Reel is login-walled: only the oEmbed thumbnail and caption were accessible.'],
      errors: [{ tool: 'ytdlp', op: 'download', error_type: 'http_error', message: 'HTTP 401: login required', attempts: 2, at: iso(BASE_MS + 900) }],
    }),
    provenance: provenance({
      first_seen_estimate: { url: mockUrl('mock-gaming-clips/videos/convoy-airbase-sim'), page_title: 'Airbase defence: simulation gameplay (2023)', first_seen_at: day(2023, 5, 14), timestamp_source: 'article:published_time', match_type: 'partial', domain: 'example.org', context_flags: [] },
      appearances: [
        { url: mockUrl('mock-gaming-clips/videos/convoy-airbase-sim'), page_title: 'Airbase defence: simulation gameplay (2023)', first_seen_at: day(2023, 5, 14), timestamp_source: 'article:published_time', match_type: 'partial', domain: 'example.org', context_flags: [] },
        { url: mockUrl('mock-forum/threads/airbase-night-attack'), page_title: 'Night attack on airbase?? (forum repost)', first_seen_at: day(2024, 2, 3), timestamp_source: 'http_last_modified', match_type: 'full', domain: 'example.org', context_flags: [] },
        { url: mockUrl('mock-factcheck-desk/fact-check/airport-missile-video'), page_title: 'Fact check: viral missile-strike video', first_seen_at: day(2026, 10, 31, 6), timestamp_source: 'article:published_time', match_type: 'page', domain: 'example.org', context_flags: [] },
      ],
      out_of_context_score: 0.82,
      items: [falseSources[4]!],
      notes: ['Matched on the oEmbed thumbnail via reverse image search.'],
    }),
    claim: claim('Video shows a missile strike on Mockpur airport last night.', {
      caption_text: 'Missile strike on Mockpur airport last night!! Media is hiding this. Share before deleted',
    }),
    input: input({
      platform: 'instagram', input_kind: 'url', url: mockUrl('instagram-reel/C0ffee'), title: 'Reel by @mock_breaking_now',
      fetch_methods: ['oembed', 'yt-dlp (failed: login required)'], labels: ['Visual forensics unavailable — analyzed caption only'],
      media_hashes: [{ id: 'M1', kind: 'thumbnail', sha256: '9b1c3f0e5a7d2c4b6e8f0a1b3c5d7e9f1a2b4c6d8e0f1a3b5c7d9e1f2a4b6c8d', phash: 'c3a1f0e4b2d69871', frame_ts: null }],
    }),
    fusion: fusion({
      candidate: 'FALSE', evidence_state: 'refutes', truth_score: -0.86, p_true: 0.12, groups_refute: 4,
      signals: [
        signal('claimreview', -0.87, 0.35, { detail: '"False" from Mock Fact Check Desk, similarity 0.87', evidence_ids: ['E1'] }),
        signal('news_stance', -1, 0.25, { detail: '0 supporting groups, 3 refuting', evidence_ids: ['E2', 'E3', 'E4'] }),
        signal('gdelt', 0, 0.1, { present: false, detail: 'No coverage of an airport strike (absent, never negative)' }),
        signal('wikidata', 0, 0.1, { present: false, detail: 'No entity facts to compare' }),
        signal('provenance', -0.57, 0.2, { detail: 'Earliest appearance predates the claimed event (out-of-context 0.82)', evidence_ids: ['P1'] }),
      ],
    }),
    debate: debate({
      judge: { label: 'FALSE', rationale: [], provider: 'mock', model_id: 'mock/gpt-6-luna', extractive: false },
      verifier: { ok: true, checks: [], failing_sentences: [], methods: ['deterministic', 'llm', 'nli'] },
      second_judge: { provider: 'mock', model_id: 'mock/gpt-5.6-luna', label: 'FALSE', agrees: true, error: null },
      latency_ms: 3100,
    }),
    labels: ['Visual forensics unavailable — analyzed caption only'],
    report_eligible: true,
    legal_eligible: false,
    legal_block_reason: 'Legal drafts are only offered when the verdict is MANIPULATED and the media depicts an identifiable individual.',
    audit_id: 102,
  }),
}

// ---- (c) MANIPULATED -------------------------------------------------------------------------

const manipSources = [
  ev({
    id: 'E1', source: 'Mock Fact Check Desk', group: 'mock-factcheck-desk', slug: 'fact-check/minister-exam-video',
    title: 'Fact check: Video of minister cancelling board exams is a deepfake',
    snippet: 'The video is digitally altered: the audio was synthesized and lip movements do not match. The original speech, from March, was about school infrastructure.',
    quote: 'the audio was synthesized and lip movements do not match',
    stance: 'refute', method: 'factcheck_claimsearch', tool: 'factcheck', published: day(2026, 10, 30), relevance: 0.94,
    factCheck: { publisher: 'Mock Fact Check Desk', rating_text: 'Altered video', normalized_rating: -1, manipulation_rating: true, claim_text: 'Minister announces cancellation of board exams', claimant: 'Viral video', similarity: 0.91 },
  }),
  ev({
    id: 'E2', source: 'Mock Wire A', group: 'mock-wire-a', slug: 'minister-office-deepfake',
    title: 'Minister’s office files complaint over fabricated video',
    snippet: 'The minister’s office said the clip was fabricated using AI and has filed a complaint with the cyber crime cell. Board exams will be held as scheduled.',
    quote: 'the clip was fabricated using AI',
    stance: 'refute', method: 'wire_rss', tool: 'wire_rss', published: day(2026, 10, 30, 14), relevance: 0.9,
  }),
  ev({
    id: 'E3', source: 'Education department release (mock)', group: 'mock-gov-press', slug: 'release/board-exams-schedule',
    snippet: 'Board examinations will be conducted as per the published schedule. A video in circulation is fake.',
    quote: 'Board examinations will be conducted as per the published schedule',
    stance: 'refute', method: 'pib_rss', tool: 'pib_rss', published: day(2026, 10, 30, 16), relevance: 0.92, gov: true,
  }),
  ev({
    id: 'P1', source: 'Known-fake index (seed manifest)', group: 'mock-known-fakes', slug: 'known-fakes/KF-MOCK-0007',
    title: 'KF-MOCK-0007: doctored minister video',
    snippet: 'Seeded known fake: lip-synced deepfake of a state minister, first flagged 29 Oct 2026.',
    stance: 'refute', method: 'phash_index', tool: 'phash_index', published: day(2026, 10, 29), relevance: 0.97,
  }),
]

const manipulatedScenario: Scenario = {
  key: 'manipulated',
  title: 'MANIPULATED: deepfake of a (fictional) minister',
  description: 'Sampled 16 frames of a 412 s video; ensemble 0.91; known-fake pHash match; legal draft offered.',
  timeline: standardTimeline({
    resolve_input: { dur: 1900 }, extract_claim: { start: 1920, dur: 2600 }, check_worthiness: { start: 4540, dur: 160 },
    evidence_branch: { start: 4720, dur: 3000 }, forensic_branch: { start: 4725, dur: 5200 }, provenance_branch: { start: 4730, dur: 1400 },
    fuse_evidence: { start: 9950, dur: 70 }, judge: { start: 10040, dur: 3300 }, decide_action: { start: 13360, dur: 20 },
    build_report: { start: 13390, dur: 240 }, audit_write: { start: 13640, dur: 150 },
  }),
  job: job({
    verdict: 'MANIPULATED',
    confidence: 0.89,
    evidence_state: 'manipulated',
    rationale: [
      r('A fact-check rates the video "Altered video": the audio was synthesized and lip movements do not match.', 'E1'),
      r('The minister’s office states the clip was fabricated using AI.', 'E2'),
      r('The frames match a seeded known fake (pHash distance 4 of 64).', 'P1'),
    ],
    cited_evidence_ids: ['E1', 'E2', 'P1', 'E3'],
    independent_source_count: 3,
    sources: manipSources,
    forensic: forensic({
      status: 'manipulated', ensemble_score: 0.91, disagreement: 0.07, faces_found: 1, frames_analyzed: 16,
      sampling: { fps: 1, frames: 16, duration_s: 412, capped: true, note: 'Sampled 16 frames of 412 s video (cap reached)' },
      models: [
        { model_id: 'mock/vit-deepfake-detector', model_version: 'mock-sha-a1', provider: 'local', raw_score: 0.94, weight: 0.4, frames_scored: 16, latency_ms: 2100, error: null },
        { model_id: 'mock/xception-ffpp', model_version: 'mock-sha-b2', provider: 'hf_inference', raw_score: 0.87, weight: 0.3, frames_scored: 16, latency_ms: 3400, error: null },
        { model_id: 'mock/commercial-deepfake-api', model_version: 'v1-mock', provider: 'sightengine', raw_score: 0.91, weight: 0.3, frames_scored: 8, latency_ms: 2900, error: null },
      ],
      metadata: {
        exif_present: false, exif_software: null, exif_stripped_suspected: true,
        reencode_hints: ['Re-encoded at least twice (H.264 → H.264)', 'Audio track sample rate differs from original broadcast'],
        container: 'mp4 (isom)', c2pa_present: false, c2pa_signer: null, c2pa_issuer: null, c2pa_actions: [],
        c2pa_note: 'Absence of C2PA Content Credentials is not evidence of manipulation.',
      },
      asr_ocr_mismatch: 0.64, synthetic_speech_score: 0.81,
      notes: ['Spoken words disagree with the on-screen ticker (mismatch 0.64).'],
      tools_used: { local: 'mock-1', hf_inference: 'mock-1', sightengine: 'mock-1' },
      latency_ms: 5200,
    }),
    provenance: provenance({
      first_seen_estimate: { url: mockUrl('mock-video-site/watch/minister-exams'), page_title: 'BREAKING minister cancels exams', first_seen_at: day(2026, 10, 29, 22), timestamp_source: 'known_fake_manifest', match_type: 'phash', domain: 'example.org', context_flags: ['known_disinfo'] },
      appearances: [
        { url: mockUrl('mock-video-site/watch/minister-exams'), page_title: 'BREAKING minister cancels exams', first_seen_at: day(2026, 10, 29, 22), timestamp_source: 'known_fake_manifest', match_type: 'phash', domain: 'example.org', context_flags: ['known_disinfo'] },
        { url: mockUrl('mock-state-media/clips/exams'), page_title: 'Exams cancelled? (repost)', first_seen_at: day(2026, 10, 30, 3), timestamp_source: 'gdelt', match_type: 'clip', domain: 'example.org', context_flags: ['state_affiliated'] },
        { url: mockUrl('mock-satire/news/exams-cancelled-forever'), page_title: 'Exams cancelled forever, students rejoice', first_seen_at: null, timestamp_source: 'unknown', match_type: 'page', domain: 'example.org', context_flags: ['satire'] },
      ],
      known_fake_matches: [
        { known_fake_id: 'KF-MOCK-0007', label: 'manipulated', hamming: 4, clip_similarity: 0.97, source_urls: [mockUrl('mock-factcheck-desk/fact-check/minister-exam-video')], title: 'Doctored video of minister announcing exam cancellation', notes: 'Lip-sync deepfake; audio synthesized.' },
      ],
      prior_run_matches: ['mock-manipulated-earlier'],
      context_flags: ['known_disinfo', 'state_affiliated'],
      items: [manipSources[3]!],
      tools_used: { phash_index: 'mock-1', clip_index: 'mock-1' },
      latency_ms: 1400,
    }),
    claim: claim('Video: Minister Asha Verma (fictional) announces that all board exams are cancelled.', {
      transcript: 'All board examinations this year stand cancelled with immediate effect.', transcript_language: 'en',
      ocr_text: 'LIVE | Minister reviews school infrastructure', asr_ocr_mismatch: 0.64,
      subject: { is_identifiable_individual: true, is_private_individual: false, name: 'Asha Verma (fictional)' },
    }),
    input: input({
      platform: 'youtube', input_kind: 'url', url: mockUrl('youtube-watch/abc123'), title: 'Minister cancels board exams (viral)', media_accessible: true, media_count: 16,
      sampling: { fps: 1, frames: 16, duration_s: 412, capped: true, note: 'Sampled 16 frames of 412 s video (cap reached)' },
      fetch_methods: ['oembed', 'yt-dlp'], labels: ['Sampled 16 frames of 412 s video'],
      media_hashes: [0, 26, 52, 78, 104, 130].map((t, i) => ({ id: `F${i + 1}`, kind: 'video_frame', sha256: `${(i + 10).toString(16)}d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f80`.slice(0, 64), phash: `e1c3a5f7b9d0${(i * 7 + 16).toString(16).padStart(4, '0')}`, frame_ts: t })),
    }),
    fusion: fusion({
      candidate: 'MANIPULATED', evidence_state: 'manipulated', truth_score: -0.9, manipulation_score: 0.91, p_true: 0.06, groups_refute: 3, groups_manipulation: 3,
      signals: [
        signal('claimreview', -0.91, 0.35, { detail: '"Altered video" (manipulation rating), similarity 0.91', evidence_ids: ['E1'] }),
        signal('news_stance', -1, 0.25, { detail: '3 groups refute / document fabrication', evidence_ids: ['E2', 'E3'] }),
        signal('gdelt', 0, 0.1, { present: false, detail: 'No coverage of an exam cancellation' }),
        signal('wikidata', 1, 0.1, { detail: 'Person holds the stated office (fictional entity in mock)' }),
        signal('provenance', -1, 0.2, { detail: 'Known-fake match KF-MOCK-0007', evidence_ids: ['P1'] }),
        signal('forensic', 0.91, 0, { detail: 'Ensemble P(fake) 0.91, conclusive', reliability: 0.9 }),
      ],
    }),
    debate: debate({
      judge: { label: 'MANIPULATED', rationale: [], provider: 'mock', model_id: 'mock/gpt-6-luna', extractive: false },
      verifier: { ok: true, checks: [], failing_sentences: [], methods: ['deterministic', 'llm', 'nli'] },
      second_judge: { provider: 'mock', model_id: 'mock/gpt-5.6-luna', label: 'MANIPULATED', agrees: true, error: null },
      latency_ms: 3300,
    }),
    labels: ['Sampled 16 frames of 412 s video'],
    report_eligible: true,
    legal_eligible: true,
    audit_id: 103,
  }),
}

// ---- Abstain: CONFLICT + forensic inconclusive -----------------------------------------------

const conflictSources = [
  ev({
    id: 'E1', source: 'Mock Daily B', group: 'mock-daily-b', slug: 'city/ring-road-two-wheeler-ban',
    snippet: 'The municipal corporation has notified a ban on two-wheelers on the Ring Road from Monday, according to a circular seen by this newspaper.',
    quote: 'notified a ban on two-wheelers on the Ring Road from Monday',
    stance: 'support', method: 'google_news_rss', tool: 'gnews_rss', published: day(2026, 10, 30), relevance: 0.86,
  }),
  ev({
    id: 'E2', source: 'Mock Regional C', group: 'mock-regional-c', slug: 'ring-road-ban',
    snippet: 'Commuters say traffic police have started informing riders about the upcoming restriction on Ring Road.',
    quote: 'started informing riders about the upcoming restriction',
    stance: 'support', method: 'serper_news', tool: 'serper', published: day(2026, 10, 30, 15), relevance: 0.7, stanceConfidence: 0.62,
  }),
  ev({
    id: 'E3', source: 'Mock Wire A', group: 'mock-wire-a', slug: 'ring-road-proposal-not-approved',
    snippet: 'A corporation spokesperson said the proposal to restrict two-wheelers is still a draft and has not been approved.',
    quote: 'is still a draft and has not been approved',
    stance: 'refute', method: 'wire_rss', tool: 'wire_rss', published: day(2026, 10, 31, 4), relevance: 0.88,
  }),
  ev({
    id: 'E4', source: 'Municipal notice board (mock)', group: 'mock-gov-press', slug: 'notices/draft-traffic-order',
    snippet: 'Draft traffic order published for public comments until 15 November. No restriction is in force.',
    quote: 'No restriction is in force',
    stance: 'refute', method: 'serper_web', tool: 'serper', published: day(2026, 10, 28), relevance: 0.84, gov: true,
  }),
]

const abstainScenario: Scenario = {
  key: 'abstain',
  title: 'UNVERIFIABLE: sources conflict (ambiguous input)',
  description: '2 groups support, 2 refute; forensic detectors disagree beyond the delta; cheap abstain (debate skipped).',
  timeline: standardTimeline({ judge: { dur: 30, status: 'skipped', detail: 'fusion candidate UNVERIFIABLE' }, decide_action: { start: 5650 }, build_report: { start: 5680 }, audit_write: { start: 5900 } }),
  job: job({
    verdict: 'UNVERIFIABLE',
    confidence: 0.52,
    abstain_reason: 'CONFLICT',
    abstain_detail: 'Two independent groups support the claim (E1, E2) and two refute it (E3, E4); neither side dominates.',
    evidence_state: 'conflicting',
    rationale: [
      r('A newspaper reports a notified two-wheeler ban from Monday.', 'E1'),
      r('The corporation spokesperson says the proposal is still a draft and not approved.', 'E3'),
      r('The municipal notice board lists only a draft order open for public comment.', 'E4'),
    ],
    cited_evidence_ids: ['E1', 'E2', 'E3', 'E4'],
    independent_source_count: 4,
    sources: conflictSources,
    forensic: forensic({
      status: 'inconclusive', ensemble_score: 0.44, disagreement: 0.48, frames_analyzed: 1,
      models: [
        { model_id: 'mock/vit-deepfake-detector', model_version: 'mock-sha-a1', provider: 'local', raw_score: 0.66, weight: 0.4, frames_scored: 1, latency_ms: 300, error: null },
        { model_id: 'mock/xception-ffpp', model_version: 'mock-sha-b2', provider: 'hf_inference', raw_score: 0.18, weight: 0.3, frames_scored: 1, latency_ms: 900, error: null },
        { model_id: 'mock/commercial-deepfake-api', model_version: 'v1-mock', provider: 'sightengine', raw_score: 0.42, weight: 0.3, frames_scored: 1, latency_ms: 1200, error: null },
      ],
      metadata: {
        exif_present: false, exif_software: null, exif_stripped_suspected: true, reencode_hints: ['JPEG quality ~38: messaging-app recompression'],
        container: 'jpeg', c2pa_present: false, c2pa_signer: null, c2pa_issuer: null, c2pa_actions: [],
        c2pa_note: 'Absence of C2PA Content Credentials is not evidence of manipulation.',
      },
      notes: ['Scores disagree by 0.48 (> delta 0.40): forensic branch is inconclusive.'],
    }),
    provenance: provenance({ branch_status: 'insufficient', notes: ['No prior appearances found for this image.'] }),
    claim: claim('Mockpur municipal corporation has banned two-wheelers on the Ring Road from Monday.', {
      ocr_text: 'NOTICE: Two-wheelers prohibited on Ring Road w.e.f. Monday', check_worthiness: 0.79,
    }),
    input: input({ platform: 'upload', input_kind: 'upload', title: 'whatsapp-image-2026-10-30.jpg', media_accessible: true, media_count: 1, labels: ['Image heavily recompressed (messaging-app forward)'],
      media_hashes: [{ id: 'M1', kind: 'image', sha256: '4f2a9c1d7e3b5a8f0c6d2e9b1a7f3c5d8e0b2a4c6f8d1e3a5b7c9d0f2e4a6c8b', phash: '8f0f0e1c3c7e7c38', frame_ts: null }] }),
    fusion: fusion({
      candidate: 'UNVERIFIABLE', candidate_reason: 'CONFLICT', evidence_state: 'conflicting', truth_score: 0.04, p_true: 0.52, groups_support: 2, groups_refute: 2,
      signals: [
        signal('claimreview', 0, 0.35, { present: false, detail: 'No ClaimReview match (absent)' }),
        signal('news_stance', 0, 0.25, { detail: '2 supporting groups vs 2 refuting', evidence_ids: ['E1', 'E2', 'E3', 'E4'] }),
        signal('gdelt', 0.2, 0.1, { detail: 'Light local coverage' }),
        signal('wikidata', 0, 0.1, { present: false }),
        signal('provenance', 0, 0.2, { present: false, detail: 'No prior appearances' }),
      ],
    }),
    debate: debate({ skipped: true, skip_reason: 'Fusion candidate is UNVERIFIABLE (CONFLICT): cheap abstain, debate not needed.' }),
    labels: ['Image heavily recompressed (messaging-app forward)'],
    audit_id: 104,
  }),
}

// ---- Abstain: NO_EVIDENCE (G8) ---------------------------------------------------------------

const noEvidenceScenario: Scenario = {
  key: 'no_evidence',
  title: 'UNVERIFIABLE: no evidence found (G8)',
  description: 'Nothing relevant anywhere. Must NOT look like FALSE.',
  timeline: standardTimeline({
    forensic_branch: { dur: 30, status: 'skipped', detail: 'text-only input' },
    provenance_branch: { dur: 25, status: 'skipped', detail: 'no media' },
    evidence_branch: { dur: 4100 },
    fuse_evidence: { start: 6460 }, judge: { start: 6530, dur: 20, status: 'skipped', detail: 'fusion candidate UNVERIFIABLE' },
    decide_action: { start: 6560 }, build_report: { start: 6580 }, audit_write: { start: 6800 },
  }),
  job: job({
    source: 'pwa',
    verdict: 'UNVERIFIABLE',
    confidence: 0.5,
    abstain_reason: 'NO_EVIDENCE',
    abstain_detail: 'Fact-check registry, Google News, wire RSS, government releases and GDELT returned no relevant items for this claim.',
    evidence_state: 'no_evidence',
    independent_source_count: 0,
    sources: [],
    forensic: forensic({ branch_status: 'skipped', status: 'unavailable', notes: ['Text-only input.'] }),
    provenance: provenance({ branch_status: 'skipped' }),
    claim: claim('Every student in Mockpur district will receive a free laptop under a new scheme if they forward this message to 10 groups.', {
      caption_text: 'Forwarded many times: Govt FREE LAPTOP scheme for all students of Mockpur!! Forward to 10 groups to register.',
      check_worthiness: 0.74,
    }),
    input: input({ platform: 'text', input_kind: 'text' }),
    fusion: fusion({
      candidate: 'UNVERIFIABLE', candidate_reason: 'NO_EVIDENCE', evidence_state: 'no_evidence', p_true: 0.5,
      signals: [
        signal('claimreview', 0, 0.35, { present: false, detail: 'No ClaimReview match (absent, never negative)' }),
        signal('news_stance', 0, 0.25, { present: false, detail: 'No relevant news items' }),
        signal('gdelt', 0, 0.1, { present: false, detail: 'No coverage (absent, never negative)' }),
        signal('wikidata', 0, 0.1, { present: false }),
        signal('provenance', 0, 0.2, { present: false }),
      ],
      notes: ['All signals absent: no evidence is not evidence of falsity (E4).'],
    }),
    debate: debate({ skipped: true, skip_reason: 'No evidence to debate.' }),
    audit_id: 105,
  }),
}

// ---- NOT_CHECKABLE (E3) ----------------------------------------------------------------------

const notCheckableScenario: Scenario = {
  key: 'not_checkable',
  title: 'NOT_CHECKABLE: cooking reel (E3)',
  description: 'Cheap gate: no factual claim; branches never run.',
  timeline: [
    ['resolve_input', 0, 1300],
    ['extract_claim', 1320, 1900],
    ['check_worthiness', 3240, 120],
    ['decide_action', 3380, 10],
    ['build_report', 3400, 60],
    ['audit_write', 3470, 120],
  ],
  job: job({
    verdict: 'NOT_CHECKABLE',
    confidence: null,
    abstain_detail: 'The video is a cooking tutorial ("Crispy dosa in 10 minutes") with no factual claim about people, events or statistics.',
    evidence_state: 'not_checkable',
    claim: {
      ...claim('', { check_worthiness: 0.07, worthiness_reason: 'Cooking tutorial; instructions and opinions only.' }),
      claims: [],
      primary_claim_id: null,
      transcript: 'Add a cup of batter, spread it thin, and drizzle oil around the edges…',
      caption_text: 'Crispy dosa in 10 minutes \u{1F525} #recipe',
    },
    input: input({ platform: 'instagram', input_kind: 'url', url: mockUrl('instagram-reel/D0sa'), title: 'Reel by @mock_kitchen', media_accessible: true, media_count: 8, fetch_methods: ['oembed', 'yt-dlp'] }),
    audit_id: 106,
  }),
}

// ---- Degraded tools (E6) + rate-limited processing (E10) -------------------------------------

const degradedSources = [
  ev({
    id: 'E1', source: 'Weather department bulletin (mock)', group: 'mock-gov-press', slug: 'bulletins/rainfall-2026-10-29',
    snippet: 'Mockpur district recorded 112 mm of rainfall in the 24 hours ending 08:30 on 29 October.',
    quote: 'recorded 112 mm of rainfall',
    stance: 'support', method: 'pib_rss', tool: 'pib_rss', published: day(2026, 10, 29, 10), relevance: 0.94, gov: true,
  }),
  ev({
    id: 'E2', source: 'Mock Wire A', group: 'mock-wire-a', slug: 'mockpur-heavy-rain',
    snippet: 'Heavy rain lashed Mockpur, which received 112 mm of rain, the weather department said, disrupting traffic.',
    quote: 'received 112 mm of rain',
    stance: 'support', method: 'wire_rss', tool: 'wire_rss', published: day(2026, 10, 29, 13), relevance: 0.9,
  }),
]

const degradedScenario: Scenario = {
  key: 'degraded',
  title: 'TRUE (degraded): GDELT down, search unconfigured',
  description: 'Tool outage mid-run: GDELT times out, Serper has no key; queue shows "rate-limited" while waiting.',
  timeline: standardTimeline({ evidence_branch: { dur: 4800, detail: 'gdelt timeout; serper unconfigured' }, forensic_branch: { dur: 40, status: 'skipped' }, provenance_branch: { dur: 35, status: 'skipped' }, fuse_evidence: { start: 7160 }, judge: { start: 7240 }, decide_action: { start: 10360 }, build_report: { start: 10380 }, audit_write: { start: 10600 } }),
  processingDetail: { from: 2600, to: 5200, detail: 'processing (rate-limited: gnews_rss)' },
  extraEvents: [
    { at: 2600, event: { type: 'tool', tool: 'gnews_rss', status: 'rate_limited', retry_in_ms: 1500 } },
    { at: 4300, event: { type: 'tool', tool: 'gdelt', status: 'error', error_type: 'timeout', attempts: 3 } },
  ],
  job: job({
    verdict: 'TRUE',
    confidence: 0.78,
    evidence_state: 'supports',
    rationale: [
      r('The weather department bulletin records 112 mm of rainfall in Mockpur district on 29 October.', 'E1'),
      r('An independent wire report cites the same figure.', 'E2'),
    ],
    cited_evidence_ids: ['E1', 'E2'],
    independent_source_count: 2,
    sources: degradedSources,
    forensic: forensic({ branch_status: 'skipped', status: 'unavailable' }),
    provenance: provenance({ branch_status: 'skipped' }),
    claim: claim('Mockpur district recorded 112 mm of rain on 29 October, the weather department said.'),
    input: input({ platform: 'web', input_kind: 'url', url: mockUrl('news-site/rain-112mm'), title: 'Rain update (mock article)', fetch_methods: ['page_meta'] }),
    fusion: fusion({
      candidate: 'TRUE', evidence_state: 'supports', truth_score: 0.72, p_true: 0.83, groups_support: 2,
      signals: [
        signal('claimreview', 0, 0.35, { present: false, detail: 'No ClaimReview match' }),
        signal('news_stance', 1, 0.25, { detail: '2 supporting groups', evidence_ids: ['E1', 'E2'] }),
        signal('gdelt', 0, 0.1, { present: false, detail: 'GDELT unavailable (timeout x3): excluded, not negative' }),
        signal('wikidata', 1, 0.1, { detail: 'District exists' }),
        signal('provenance', 0, 0.2, { present: false }),
      ],
    }),
    debate: debate({
      judge: { label: 'TRUE', rationale: [], provider: 'mock', model_id: 'mock/gpt-6-luna', extractive: false },
      verifier: { ok: true, checks: [], failing_sentences: [], methods: ['deterministic', 'llm', 'nli'] },
      second_judge: { provider: 'mock', model_id: 'mock/gpt-5.6-luna', label: 'UNVERIFIABLE', agrees: false, error: null },
      latency_ms: 3100,
    }),
    tool_errors: [
      { tool: 'gdelt', op: 'doc_search', error_type: 'timeout', message: 'GDELT DOC API did not answer within 6 s', attempts: 3, at: iso(BASE_MS + 6900) },
      { tool: 'serper', op: 'news', error_type: 'unconfigured', message: 'SERPER_API_KEY not set', attempts: 0, at: iso(BASE_MS + 2400) },
      { tool: 'gnews_rss', op: 'search', error_type: 'rate_limited', message: 'HTTP 429; succeeded after backoff', attempts: 2, at: iso(BASE_MS + 4100) },
    ],
    degraded_tools: ['gdelt', 'serper'],
    labels: ['GDELT unavailable — result degraded', 'Second judge disagreed: confidence reduced ×0.7'],
    audit_id: 107,
  }),
}

// ---- Abstain: UNGROUNDED_RATIONALE -----------------------------------------------------------

const ungroundedSources = [
  ev({
    id: 'E1', source: 'Labour department notification (mock)', group: 'mock-gov-press', slug: 'notifications/minimum-wages-2026',
    snippet: 'Revised minimum rates of wages for construction workers are notified with effect from 1 October 2026.',
    quote: 'Revised minimum rates of wages for construction workers are notified',
    stance: 'support', method: 'pib_rss', tool: 'pib_rss', published: day(2026, 9, 28), relevance: 0.9, gov: true,
  }),
  ev({
    id: 'E2', source: 'Mock Daily B', group: 'mock-daily-b', slug: 'business/minimum-wage-hike',
    snippet: 'The revision raises daily wages for unskilled construction workers by 12%, officials said.',
    quote: 'raises daily wages for unskilled construction workers by 12%',
    stance: 'support', method: 'google_news_rss', tool: 'gnews_rss', published: day(2026, 9, 29), relevance: 0.86,
  }),
]

const rejected = [
  r('The labour department notified revised minimum wages for construction workers from 1 October 2026.', 'E1'),
  r('The increase is 15% across all construction categories.', 'E2'),
  r('Trade unions welcomed the decision.'),
]

const ungroundedScenario: Scenario = {
  key: 'ungrounded',
  title: 'UNVERIFIABLE: rationale failed the verifier',
  description: 'Judge said TRUE, but 2 of 3 rationale sentences are not supported by stored evidence.',
  timeline: standardTimeline({ forensic_branch: { dur: 35, status: 'skipped' }, provenance_branch: { dur: 30, status: 'skipped' } }),
  job: job({
    verdict: 'UNVERIFIABLE',
    confidence: 0.61,
    abstain_reason: 'UNGROUNDED_RATIONALE',
    abstain_detail: 'Verifier rejected 2 of 3 rationale sentences (sentence 2 contradicts E2: 12% not 15%; sentence 3 has no citation).',
    evidence_state: 'supports',
    rationale: [],
    cited_evidence_ids: ['E1', 'E2'],
    independent_source_count: 2,
    sources: ungroundedSources,
    forensic: forensic({ branch_status: 'skipped', status: 'unavailable' }),
    provenance: provenance({ branch_status: 'skipped' }),
    claim: claim('The state government raised the minimum wage for construction workers by 15% this month.'),
    input: input({ platform: 'facebook', input_kind: 'url', url: mockUrl('facebook-post/wage-hike'), title: 'Post by Mock Workers Forum', fetch_methods: ['page_meta', 'playwright'] }),
    fusion: fusion({ candidate: 'TRUE', evidence_state: 'supports', truth_score: 0.55, p_true: 0.79, groups_support: 2,
      signals: [signal('news_stance', 1, 0.25, { evidence_ids: ['E1', 'E2'], detail: '2 supporting groups (on "raised", not on "15%")' }), signal('claimreview', 0, 0.35, { present: false })] }),
    debate: debate({
      proponent: { role: 'proponent', points: [r('Wages were revised from 1 October.', 'E1')], followup_queries: ['minimum wage construction 15%'], followup_evidence_ids: [] },
      opponent: { role: 'opponent', points: [r('The reported increase is 12%, not 15%.', 'E2')], followup_queries: [], followup_evidence_ids: [] },
      judge: { label: 'TRUE', rationale: rejected, provider: 'mock', model_id: 'mock/gpt-6-luna', extractive: false },
      verifier: {
        ok: false, methods: ['deterministic', 'llm', 'nli'],
        failing_sentences: [rejected[1]!.text, rejected[2]!.text],
        checks: [
          { sentence_index: 0, text: rejected[0]!.text, evidence_ids: ['E1'], deterministic_ok: true, llm_supported: true, nli_entailment: 0.91, ok: true, reason: '' },
          { sentence_index: 1, text: rejected[1]!.text, evidence_ids: ['E2'], deterministic_ok: true, llm_supported: false, nli_entailment: 0.04, ok: false, reason: 'E2 says 12%, not 15% (NLI contradiction)' },
          { sentence_index: 2, text: rejected[2]!.text, evidence_ids: [], deterministic_ok: false, llm_supported: null, nli_entailment: null, ok: false, reason: 'No evidence id cited' },
        ],
      },
      second_judge: { provider: 'mock', model_id: 'mock/gpt-5.6-luna', label: 'UNVERIFIABLE', agrees: false, error: null },
      latency_ms: 3100,
    }),
    audit_id: 108,
  }),
}

export const SCENARIOS: Record<ScenarioKey, Scenario> = {
  true: trueScenario,
  false: falseScenario,
  manipulated: manipulatedScenario,
  abstain: abstainScenario,
  no_evidence: noEvidenceScenario,
  not_checkable: notCheckableScenario,
  degraded: degradedScenario,
  ungrounded: ungroundedScenario,
}

export const SCENARIO_KEYS = Object.keys(SCENARIOS) as ScenarioKey[]

/** Pick a scenario from free text (mock mode only), so typing "deepfake" demos MANIPULATED etc. */
export function guessScenario(text: string): ScenarioKey {
  const t = text.toLowerCase()
  if (/deepfake|minister|manipulat/.test(t)) return 'manipulated'
  if (/missile|strike|airport/.test(t)) return 'false'
  if (/recipe|dosa|cook/.test(t)) return 'not_checkable'
  if (/laptop|forward/.test(t)) return 'no_evidence'
  if (/ring road|ban/.test(t)) return 'abstain'
  if (/rain|weather/.test(t)) return 'degraded'
  if (/wage/.test(t)) return 'ungrounded'
  return 'true'
}
