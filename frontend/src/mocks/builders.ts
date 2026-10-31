/**
 * Builders for MOCK fixtures. Everything produced here is fictional: every URL is
 * https://example.org/mock/..., every model id starts with "mock/", place names are invented
 * ("Mockpur"), and the UI shows a MOCK DATA banner whenever these are served.
 */
import type {
  AbstainBand,
  ClaimExtraction,
  DebateResult,
  EvidenceItem,
  ForensicResult,
  FusionResult,
  InputSummary,
  JobView,
  NodeTiming,
  ProvenanceResult,
  RationaleSentence,
  RetrievalMethod,
  SignalScore,
  Stance,
} from '../api/types'

export const MOCK_ORIGIN = 'https://example.org/mock'
/** Fixed base time so fixtures are deterministic (the mock server re-bases them to "now"). */
export const BASE_MS = Date.UTC(2026, 9, 31, 8, 30, 0)
export const iso = (ms: number) => new Date(ms).toISOString()

export function mockUrl(path: string): string {
  return `${MOCK_ORIGIN}/${path.replace(/^\/+/, '')}`
}

export interface EvSpec {
  id: string
  source: string
  group: string
  slug: string
  snippet: string
  quote?: string
  stance: Stance
  method: RetrievalMethod
  tool: string
  title?: string
  published?: string
  relevance?: number
  stanceConfidence?: number
  gov?: boolean
  factCheck?: EvidenceItem['claimreview']
  branch?: string
}

export function ev(s: EvSpec): EvidenceItem {
  if (s.quote && !s.snippet.includes(s.quote)) throw new Error(`fixture ${s.id}: stance_quote must be a substring of snippet`)
  return {
    id: s.id,
    source_name: s.source,
    url: mockUrl(`${s.group}/${s.slug}`),
    domain: 'example.org',
    publisher_group: s.group,
    title: s.title ?? null,
    snippet: s.snippet,
    published_at: s.published ?? null,
    retrieved_at: iso(BASE_MS + 2_000),
    language: 'en',
    confidence_relevance: s.relevance ?? 0.8,
    stance: s.stance,
    stance_confidence: s.stanceConfidence ?? 0.85,
    stance_quote: s.quote ?? null,
    retrieval_method: s.method,
    tool: s.tool,
    tool_version: 'mock-1',
    branch: s.branch ?? (s.id.startsWith('P') ? 'provenance' : s.id.startsWith('D') ? 'debate' : 'evidence'),
    is_fact_check: Boolean(s.factCheck),
    is_government_source: Boolean(s.gov),
    claimreview: s.factCheck ?? null,
  }
}

/** [node, startOffsetMs, durationMs, status?] */
export type Span = [string, number, number, ('ok' | 'error' | 'skipped')?, string?]

export function timings(spans: Span[], base = BASE_MS): NodeTiming[] {
  return spans.map(([node, start, dur, status = 'ok', detail]) => ({
    node,
    started_at: iso(base + start),
    ended_at: iso(base + start + dur),
    duration_ms: dur,
    status,
    detail: detail ?? null,
  }))
}

export const BAND: AbstainBand = { low: 0.35, high: 0.7, delta: 0.4 }

export function forensic(p: Partial<ForensicResult>): ForensicResult {
  return {
    branch_status: 'ok',
    status: 'unavailable',
    ensemble_score: null,
    disagreement: null,
    band: BAND,
    models: [],
    faces_found: 0,
    frames_analyzed: 0,
    sampling: null,
    metadata: null,
    asr_ocr_mismatch: null,
    synthetic_speech_score: null,
    notes: [],
    errors: [],
    tools_used: {},
    latency_ms: 0,
    ...p,
  }
}

export function provenance(p: Partial<ProvenanceResult>): ProvenanceResult {
  return {
    branch_status: 'ok',
    first_seen_estimate: null,
    appearances: [],
    known_fake_matches: [],
    prior_run_matches: [],
    out_of_context_score: 0,
    context_flags: [],
    items: [],
    notes: [],
    errors: [],
    tools_used: {},
    latency_ms: 0,
    ...p,
  }
}

export function signal(name: string, score: number, weight: number, p: Partial<SignalScore> = {}): SignalScore {
  return { name, score, weight, reliability: 1, present: true, detail: '', evidence_ids: [], ...p }
}

export function fusion(p: Partial<FusionResult> & Pick<FusionResult, 'candidate' | 'evidence_state'>): FusionResult {
  return {
    truth_score: 0,
    manipulation_score: 0,
    p_true: 0.5,
    signals: [],
    groups_support: 0,
    groups_refute: 0,
    groups_manipulation: 0,
    candidate_reason: null,
    weights_version: 'weights-mock-1',
    calibration_version: 'platt-mock-1',
    thresholds: { abstain_threshold: 0.7, checkworthiness_threshold: 0.4, manipulation_min: 0.8 },
    fast_path: false,
    notes: [],
    ...p,
  }
}

export function claim(text: string, p: Partial<ClaimExtraction> = {}): ClaimExtraction {
  return {
    caption_text: null,
    transcript: null,
    transcript_language: null,
    ocr_text: null,
    language: 'en',
    claims: [{ id: 'C1', text, text_en: null, entities: [], claim_type: 'event', time_reference: null, location: null }],
    primary_claim_id: 'C1',
    check_worthiness: 0.86,
    worthiness_reason: 'Specific, verifiable factual claim about a public event.',
    subject: { is_identifiable_individual: false, is_private_individual: null, name: null },
    asr_ocr_mismatch: null,
    llm_used: true,
    notes: [],
    ...p,
  }
}

export function input(p: Partial<InputSummary> & Pick<InputSummary, 'platform' | 'input_kind'>): InputSummary {
  return {
    url: null,
    title: null,
    media_accessible: false,
    media_count: 0,
    sampling: null,
    fetch_methods: [],
    labels: [],
    media_hashes: [],
    ...p,
  }
}

export const r = (text: string, ...ids: string[]): RationaleSentence => ({ text, evidence_ids: ids })

export function debate(p: Partial<DebateResult>): DebateResult {
  return {
    skipped: false,
    skip_reason: null,
    proponent: null,
    opponent: null,
    judge: null,
    verifier: null,
    second_judge: null,
    followup_items: [],
    latency_ms: 0,
    ...p,
  }
}

export const MOCK_MODELS = { primary: 'mock/gpt-6-luna', secondary: 'mock/gpt-5.6-luna' }

export function job(p: Partial<JobView>): JobView {
  return {
    job_id: 'mock-fixture',
    status: 'completed',
    created_at: iso(BASE_MS),
    updated_at: null,
    source: 'web',
    mode: 'live',
    verdict: null,
    confidence: null,
    abstain_reason: null,
    abstain_detail: null,
    evidence_state: null,
    rationale: [],
    cited_evidence_ids: [],
    independent_source_count: 0,
    sources: [],
    forensic: null,
    provenance: null,
    claim: null,
    input: null,
    fusion: null,
    debate: null,
    trace: [],
    tool_errors: [],
    degraded_tools: [],
    labels: [],
    report_eligible: false,
    legal_eligible: false,
    legal_block_reason: null,
    audit_id: null,
    resolved_models: MOCK_MODELS,
    queue_position: null,
    status_detail: null,
    redacted: false,
    error: null,
    ...p,
  }
}

/** Standard full-pipeline timeline; `scale` stretches it, overrides replace individual spans. */
export function standardTimeline(overrides: Partial<Record<string, Partial<{ start: number; dur: number; status: 'ok' | 'error' | 'skipped'; detail: string }>>> = {}): Span[] {
  const base: Record<string, { start: number; dur: number; status?: 'ok' | 'error' | 'skipped'; detail?: string }> = {
    resolve_input: { start: 0, dur: 620 },
    extract_claim: { start: 640, dur: 1480 },
    check_worthiness: { start: 2140, dur: 180 },
    evidence_branch: { start: 2340, dur: 3150 },
    forensic_branch: { start: 2345, dur: 2380 },
    provenance_branch: { start: 2350, dur: 1720 },
    fuse_evidence: { start: 5520, dur: 60 },
    judge: { start: 5600, dur: 3100 },
    decide_action: { start: 8720, dur: 15 },
    build_report: { start: 8740, dur: 210 },
    audit_write: { start: 8960, dur: 140 },
  }
  return Object.entries(base).map(([node, v]) => {
    const o = { ...v, ...(overrides[node] ?? {}) }
    return [node, o.start, o.dur, o.status, o.detail] as Span
  })
}
