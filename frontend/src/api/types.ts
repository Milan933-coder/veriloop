/**
 * TypeScript mirror of backend/veriloop/contracts/*.py (pydantic v2).
 *
 * Conventions:
 * - Field names are the pydantic field names (= wire names). Enums are their string values.
 * - `datetime` -> ISO-8601 string. `int`/`float` -> number. `dict` -> Record.
 * - Response models: a field with a pydantic default is always serialized, so it is typed as present
 *   (non-optional). `X | None` is typed `X | null`.
 * - Request models (VerifyRequest, ReportConfirmRequest, LegalDraftRequest): defaulted fields are optional.
 * - The API middleware adds `request_id` + `latency_ms` to every JSON body (see `WithMeta`).
 *
 * Do not edit the Python contracts from here: if they change, update this file to match.
 */

// ---------------------------------------------------------------------------------------------
// enums.py
// ---------------------------------------------------------------------------------------------

export const VERDICT_LABELS = ['TRUE', 'FALSE', 'MANIPULATED', 'UNVERIFIABLE', 'NOT_CHECKABLE'] as const
export type VerdictLabel = (typeof VERDICT_LABELS)[number]

export const ABSTAIN_REASONS = [
  'NO_EVIDENCE',
  'LOW_CONFIDENCE',
  'SINGLE_SOURCE',
  'CONFLICT',
  'FORENSIC_INCONCLUSIVE',
  'UNGROUNDED_RATIONALE',
  'JUDGE_DISAGREEMENT',
  'TOOL_OUTAGE',
  'LANGUAGE_UNSUPPORTED',
] as const
export type AbstainReason = (typeof ABSTAIN_REASONS)[number]

/** What the evidence says, independent of the final label. Drives G8 in the UI. */
export const EVIDENCE_STATES = [
  'supports',
  'refutes',
  'manipulated',
  'conflicting',
  'insufficient',
  'no_evidence', // nothing relevant was found (NOT the same as refutes)
  'not_checkable',
] as const
export type EvidenceState = (typeof EVIDENCE_STATES)[number]

export const JOB_STATUSES = ['queued', 'processing', 'completed', 'failed'] as const
export type JobStatus = (typeof JOB_STATUSES)[number]

export const BRANCH_STATUSES = ['ok', 'degraded', 'insufficient', 'failed', 'skipped'] as const
export type BranchStatus = (typeof BRANCH_STATUSES)[number]

export const STANCES = ['support', 'refute', 'neutral', 'unknown'] as const
export type Stance = (typeof STANCES)[number]

export const FORENSIC_STATUSES = ['real', 'manipulated', 'inconclusive', 'unavailable'] as const
export type ForensicStatus = (typeof FORENSIC_STATUSES)[number]

export const PLATFORMS = [
  'x',
  'instagram',
  'facebook',
  'youtube',
  'tiktok',
  'telegram',
  'whatsapp',
  'web', // any other URL
  'upload',
  'text',
] as const
export type Platform = (typeof PLATFORMS)[number]

export const INPUT_KINDS = ['url', 'upload', 'text'] as const
export type InputKind = (typeof INPUT_KINDS)[number]

export const MEDIA_KINDS = ['image', 'video_frame', 'thumbnail'] as const
export type MediaKind = (typeof MEDIA_KINDS)[number]

export const RETRIEVAL_METHODS = [
  'factcheck_claimsearch',
  'factcheck_imagesearch',
  'google_news_rss',
  'wire_rss',
  'pib_rss',
  'gdelt_doc',
  'serper_web',
  'serper_news',
  'wikidata_sparql',
  'vision_web_detection',
  'phash_index',
  'clip_index',
  'followup_search',
] as const
export type RetrievalMethod = (typeof RETRIEVAL_METHODS)[number]

export const AUDIT_EVENT_TYPES = ['verdict', 'report_prepared', 'report_confirmed', 'legal_draft_generated'] as const
export type AuditEventType = (typeof AUDIT_EVENT_TYPES)[number]

export const TOOL_STATES = ['ok', 'degraded', 'open', 'unconfigured', 'disabled', 'chaos'] as const
export type ToolState = (typeof TOOL_STATES)[number]

/** ISO-8601 datetime string as serialized by pydantic. */
export type IsoDateTime = string

// ---------------------------------------------------------------------------------------------
// common.py
// ---------------------------------------------------------------------------------------------

/** A logged tool failure (G3). Never replaced by fabricated data. */
export interface ToolError {
  tool: string
  op: string
  /** timeout|http_error|circuit_open|unconfigured|replay_miss|parse_error|chaos|refusal|rate_limited|exception */
  error_type: string
  message: string
  attempts: number
  at: IsoDateTime
}

export interface NodeTiming {
  node: string
  started_at: IsoDateTime
  ended_at: IsoDateTime | null
  duration_ms: number | null
  /** ok|error|skipped */
  status: string
  detail: string | null
}

export interface SamplingInfo {
  fps: number
  frames: number
  duration_s: number | null
  capped: boolean
  /** e.g. "Sampled 16 of 412 s at 1 fps (cap reached)" */
  note: string | null
}

// ---------------------------------------------------------------------------------------------
// evidence.py
// ---------------------------------------------------------------------------------------------

export interface ClaimReviewInfo {
  publisher: string
  /** textualRating as published */
  rating_text: string
  /** -1 (false) .. +1 (true); 0 = mixed / unrated */
  normalized_rating: number
  manipulation_rating: boolean
  claim_text: string | null
  claimant: string | null
  /** 0..1, claim vs reviewed claim */
  similarity: number
}

/** One citable piece of evidence. URLs are copied from tool responses and never synthesized. */
export interface EvidenceItem {
  /** unique within a job: E1.. (evidence), P1.. (provenance), D1.. (debate follow-up) */
  id: string
  source_name: string
  url: string
  /** registrable domain, e.g. "reuters.com" */
  domain: string
  /** independence key: syndicated copies share a group */
  publisher_group: string
  title: string | null
  snippet: string
  published_at: IsoDateTime | null
  retrieved_at: IsoDateTime
  language: string | null
  /** 0..1 */
  confidence_relevance: number
  stance: Stance
  stance_confidence: number
  /** must be an exact substring of snippet */
  stance_quote: string | null
  retrieval_method: RetrievalMethod
  tool: string
  tool_version: string
  /** evidence|provenance|debate */
  branch: string
  is_fact_check: boolean
  is_government_source: boolean
  claimreview: ClaimReviewInfo | null
}

export interface BranchResult {
  /** evidence|forensic|provenance */
  branch: string
  status: BranchStatus
  items: EvidenceItem[]
  errors: ToolError[]
  /** tool -> version */
  tools_used: Record<string, string>
  latency_ms: number
  notes: string[]
  fast_path_hit: boolean
}

// ---------------------------------------------------------------------------------------------
// media.py (MediaAsset.path is never exposed via the API, so MediaAsset is not mirrored)
// ---------------------------------------------------------------------------------------------

export interface Entity {
  name: string
  /** person|org|place|event|other */
  type: string
  wikidata_id: string | null
}

export interface Claim {
  /** C1, C2 ... */
  id: string
  /** in the original language */
  text: string
  /** English rendering for search (E9) */
  text_en: string | null
  entities: Entity[]
  /** event|statistic|quote|identity|other */
  claim_type: string
  time_reference: string | null
  location: string | null
}

export interface SubjectInfo {
  is_identifiable_individual: boolean
  /** null = unknown -> treated as private (G9 caution) */
  is_private_individual: boolean | null
  name: string | null
}

export interface ClaimExtraction {
  caption_text: string | null
  transcript: string | null
  transcript_language: string | null
  ocr_text: string | null
  /** ISO 639-1 of the primary claim */
  language: string
  claims: Claim[]
  primary_claim_id: string | null
  /** 0..1 */
  check_worthiness: number
  worthiness_reason: string
  subject: SubjectInfo
  /** 0..1, higher = spoken words disagree with on-screen text */
  asr_ocr_mismatch: number | null
  llm_used: boolean
  notes: string[]
}

// ---------------------------------------------------------------------------------------------
// analysis.py
// ---------------------------------------------------------------------------------------------

export interface ModelScore {
  /** exact HF repo id or commercial model name */
  model_id: string
  /** HF commit sha / API version */
  model_version: string
  /** local|hf_inference|sightengine */
  provider: string
  /** probability the media is fake, 0..1 */
  raw_score: number | null
  weight: number
  frames_scored: number
  latency_ms: number
  error: string | null
}

export interface AbstainBand {
  low: number
  high: number
  delta: number
}

export interface MetadataReport {
  exif_present: boolean
  exif_software: string | null
  exif_stripped_suspected: boolean
  reencode_hints: string[]
  container: string | null
  c2pa_present: boolean
  c2pa_signer: string | null
  c2pa_issuer: string | null
  c2pa_actions: string[]
  c2pa_note: string
}

export interface ForensicResult {
  branch_status: BranchStatus
  status: ForensicStatus
  /** weighted P(fake) */
  ensemble_score: number | null
  /** max - min across model scores */
  disagreement: number | null
  band: AbstainBand
  models: ModelScore[]
  faces_found: number
  frames_analyzed: number
  sampling: SamplingInfo | null
  metadata: MetadataReport | null
  asr_ocr_mismatch: number | null
  synthetic_speech_score: number | null
  notes: string[]
  errors: ToolError[]
  tools_used: Record<string, string>
  latency_ms: number
}

export interface Appearance {
  url: string
  page_title: string | null
  first_seen_at: IsoDateTime | null
  /** article:published_time|http_last_modified|gdelt|known_fake_manifest|unknown */
  timestamp_source: string
  /** full|partial|page|phash|clip */
  match_type: string
  domain: string | null
  /** satire|state_affiliated|known_disinfo */
  context_flags: string[]
}

export interface KnownFakeMatch {
  known_fake_id: string
  /** manipulated|out_of_context|authentic_reference */
  label: string
  hamming: number | null
  clip_similarity: number | null
  /** fact-check URLs from the seed manifest */
  source_urls: string[]
  title: string | null
  notes: string | null
}

export interface ProvenanceResult {
  branch_status: BranchStatus
  first_seen_estimate: Appearance | null
  appearances: Appearance[]
  known_fake_matches: KnownFakeMatch[]
  /** job ids with near-duplicate media */
  prior_run_matches: string[]
  /** 0..1; earliest appearance predates the claimed event */
  out_of_context_score: number
  context_flags: string[]
  /** citable pages (ids P1..) */
  items: EvidenceItem[]
  notes: string[]
  errors: ToolError[]
  tools_used: Record<string, string>
  latency_ms: number
}

// ---------------------------------------------------------------------------------------------
// reasoning.py
// ---------------------------------------------------------------------------------------------

export interface SignalScore {
  /** claimreview|news_stance|gdelt|wikidata|provenance|forensic */
  name: string
  /** -1 (refutes) .. +1 (supports); forensic uses P(fake) on the manipulation axis */
  score: number
  weight: number
  reliability: number
  /** absent signals are excluded from the average, never counted as negative (E4) */
  present: boolean
  detail: string
  evidence_ids: string[]
}

export interface FusionResult {
  /** T in [-1, 1] */
  truth_score: number
  /** M in [0, 1] */
  manipulation_score: number
  /** calibrated P(claim true) */
  p_true: number
  signals: SignalScore[]
  groups_support: number
  groups_refute: number
  groups_manipulation: number
  candidate: VerdictLabel
  candidate_reason: AbstainReason | null
  evidence_state: EvidenceState
  weights_version: string
  calibration_version: string
  thresholds: Record<string, number>
  fast_path: boolean
  notes: string[]
}

export interface RationaleSentence {
  text: string
  evidence_ids: string[]
}

export interface Argument {
  /** proponent|opponent */
  role: string
  points: RationaleSentence[]
  followup_queries: string[]
  followup_evidence_ids: string[]
}

export interface JudgeOutput {
  label: VerdictLabel
  rationale: RationaleSentence[]
  provider: string
  model_id: string
  /** true when LLM_PROVIDER=none and the rationale is quoted evidence */
  extractive: boolean
}

export interface GroundingCheck {
  sentence_index: number
  text: string
  evidence_ids: string[]
  /** ids present, ids exist in this job, urls belong to this job */
  deterministic_ok: boolean
  llm_supported: boolean | null
  /** local NLI cross-check (independent of the LLM) */
  nli_entailment: number | null
  ok: boolean
  reason: string
}

export interface VerifierOutput {
  ok: boolean
  checks: GroundingCheck[]
  failing_sentences: string[]
  /** deterministic|llm|nli */
  methods: string[]
}

export interface SecondJudge {
  provider: string
  model_id: string
  label: VerdictLabel | null
  agrees: boolean | null
  error: string | null
}

export interface DebateResult {
  skipped: boolean
  skip_reason: string | null
  proponent: Argument | null
  opponent: Argument | null
  judge: JudgeOutput | null
  verifier: VerifierOutput | null
  second_judge: SecondJudge | null
  /** ids D1.. added during debate */
  followup_items: EvidenceItem[]
  latency_ms: number
}

// ---------------------------------------------------------------------------------------------
// api.py
// ---------------------------------------------------------------------------------------------

export type Source = 'web' | 'pwa' | 'telegram' | 'api' | 'extension'

/** POST /verify (JSON body, or multipart with `file` + these fields as form fields). */
export interface VerifyRequest {
  url?: string | null
  text?: string | null
  /** auto-detected when omitted */
  platform?: Platform | null
  locale?: string
  source?: Source
  /** set by the API when a file is uploaded via multipart */
  upload_id?: string | null
}

export interface VerifyAccepted {
  job_id: string
  status: JobStatus
  status_url: string
  events_url: string
  queue_position: number
}

/** One entry of InputSummary.media_hashes (typed `list[dict]` in the contract). */
export interface MediaHash {
  id?: string
  kind?: string
  sha256?: string
  phash?: string | null
  frame_ts?: number | null
  [key: string]: unknown
}

export interface InputSummary {
  platform: Platform
  input_kind: string
  /** null after Telegram TTL redaction */
  url: string | null
  title: string | null
  media_accessible: boolean
  media_count: number
  sampling: SamplingInfo | null
  fetch_methods: string[]
  labels: string[]
  media_hashes: MediaHash[]
}

/** GET /verify/{job_id}. Also persisted as jobs.result_json. */
export interface JobView {
  job_id: string
  status: JobStatus
  created_at: IsoDateTime
  updated_at: IsoDateTime | null
  source: Source
  /** live|record|replay */
  mode: string
  verdict: VerdictLabel | null
  confidence: number | null
  abstain_reason: AbstainReason | null
  abstain_detail: string | null
  evidence_state: EvidenceState | null
  rationale: RationaleSentence[]
  cited_evidence_ids: string[]
  independent_source_count: number
  /** cited first, then the rest */
  sources: EvidenceItem[]
  forensic: ForensicResult | null
  provenance: ProvenanceResult | null
  /** redacted after Telegram TTL */
  claim: ClaimExtraction | null
  input: InputSummary | null
  fusion: FusionResult | null
  debate: DebateResult | null
  trace: NodeTiming[]
  tool_errors: ToolError[]
  degraded_tools: string[]
  labels: string[]
  report_eligible: boolean
  legal_eligible: boolean
  legal_block_reason: string | null
  audit_id: number | null
  resolved_models: Record<string, string>
  queue_position: number | null
  /** e.g. "processing (rate-limited: serper)" */
  status_detail: string | null
  redacted: boolean
  error: string | null
}

// ---- Reporting ------------------------------------------------------------------------------

export type ReportMethod = 'playwright_guided' | 'report_card' | 'deeplink_guided'

export interface PlatformPlan {
  platform: string
  target_url: string | null
  method: ReportMethod
  /** prepared|confirmed */
  status: string
  evidence_attached: boolean
  steps: string[]
  prefilled_text: string
  report_category: string | null
  card_url: string | null
}

export interface EvidenceBundle {
  verdict: Record<string, unknown>
  sources: Record<string, unknown>[]
  forensic_report: Record<string, unknown> | null
  media_hashes: Record<string, unknown>[]
  timestamps: Record<string, string>
  audit_id: number | null
  pdf_url: string
  verify_url: string
}

/** POST /report/{job_id} -> draft package (no submission). */
export interface ReportDraftPackage {
  job_id: string
  eligible: boolean
  ineligible_reason: string | null
  /** exactly what will be sent */
  preview_text: string
  bundle: EvidenceBundle | null
  bundle_url: string | null
  platform_plans: PlatformPlan[]
  /** audit row for report_prepared */
  audit_id: number | null
}

/** POST /report/{job_id}/confirm */
export interface ReportConfirmRequest {
  platform: string
  /** must be true: the user clicked "I confirm" */
  confirmed: boolean
}

export interface ReportConfirmResponse {
  attempt_logged: boolean
  attempt_id: string
  platform: string
  method: ReportMethod
  /** command for the presenter-side Playwright helper */
  reporter_agent_command: string | null
  audit_id: number | null
}

// ---- Legal ----------------------------------------------------------------------------------

export type Jurisdiction = 'IN' | 'EU' | 'US'

export interface LegalAcknowledgements {
  is_subject_or_authorized: boolean
  not_legal_advice: boolean
  no_auto_send: boolean
  will_review_before_filing: boolean
}

/** POST /legal/{job_id}/draft */
export interface LegalDraftRequest {
  jurisdiction: Jurisdiction
  acknowledgements: LegalAcknowledgements
  platform?: string | null
}

export interface LegalDraftResponse {
  draft_text: string
  jurisdiction: Jurisdiction
  checklist: string[]
  filing_instructions: string[]
  placeholders: string[]
  template_version: string
  disclaimer: string
  evidence_bundle_ref: string
  audit_id: number | null
}

// ---- Audit ----------------------------------------------------------------------------------

/** GET /audit/{job_id} */
export interface AuditEntryView {
  id: number
  job_id: string
  event_type: AuditEventType
  created_at: IsoDateTime
  prev_hash: string
  payload_hash: string
  /** canonical JSON text exactly as hashed */
  payload_json: string
  resolved_model_ids: Record<string, string>
  tool_versions: Record<string, string>
  /** base64 Ed25519 over `${prev_hash}:${payload_hash}` */
  signature: string
  key_fingerprint: string
}

/** GET /audit/{job_id}/verify */
export interface AuditVerifyResult {
  ok: boolean
  checked_rows: number
  broken_at: number | null
  reason: string | null
  head_hash: string | null
  job_entry_ids: number[]
}

// ---- Health ---------------------------------------------------------------------------------

export interface ToolStatusView {
  configured: boolean
  state: ToolState
  version: string
  calls: number
  failures: number
  last_error: string | null
}

/** GET /health */
export interface HealthView {
  status: 'ok' | 'degraded'
  /** live|record|replay */
  mode: string
  version: string
  resolved_models: Record<string, string>
  tool_status: Record<string, ToolStatusView>
  db: string
  /** model -> loaded|loading|unavailable|disabled */
  ml: Record<string, string>
  /** aicredits|none ("none" = extractive mode, no LLM calls) */
  llm_provider: string
}

// ---------------------------------------------------------------------------------------------
// Transport-level shapes (not pydantic contracts)
// ---------------------------------------------------------------------------------------------

/** Every JSON response body gets these from the API middleware. */
export interface ResponseMeta {
  request_id?: string
  latency_ms?: number
}
export type WithMeta<T> = T & ResponseMeta

/** RFC 7807 problem+json error body. */
export interface ProblemDetails {
  type?: string
  title?: string
  status?: number
  detail?: string
  instance?: string
  request_id?: string
}

/**
 * SSE `GET /verify/{id}/events` payloads. Node events are the documented ones; anything else is
 * kept as a generic event and rendered in the event log.
 */
export interface NodeEvent {
  type: 'node'
  node: string
  status: 'start' | 'end' | 'error'
  duration_ms?: number | null
  [key: string]: unknown
}
export interface GenericJobEvent {
  type: string
  [key: string]: unknown
}
export type JobEvent = NodeEvent | GenericJobEvent
