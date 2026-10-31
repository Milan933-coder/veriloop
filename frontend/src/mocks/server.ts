/**
 * In-browser MOCK backend (dev only, or builds with VITE_MOCK=1). Implements the same endpoints the
 * real API exposes, from the fictional fixtures in scenarios.ts, plus a simulated SSE trace.
 *
 * Dev knobs (URL params, mock mode only): ?sse=fail (drop the stream -> polling fallback),
 * ?health=replay|extractive|degraded, ?tamper=1 (audit verify reports a broken chain).
 */
import { ApiError } from '../api/errors'
import type { JobEventHandlers } from '../api/events'
import type {
  AuditEntryView,
  AuditVerifyResult,
  HealthView,
  JobEvent,
  JobView,
  Jurisdiction,
  LegalDraftRequest,
  LegalDraftResponse,
  PlatformPlan,
  ReportConfirmRequest,
  ReportConfirmResponse,
  ReportDraftPackage,
  VerifyAccepted,
  VerifyRequest,
} from '../api/types'
import { mockFlag } from '../lib/mockMode'
import { PLATFORM_LABELS } from '../lib/platform'
import { MOCK_MODELS, iso } from './builders'
import { SCENARIOS, guessScenario, type Scenario, type ScenarioKey } from './scenarios'

const QUEUE_MS = 700
const ID_RE = /^mock-([a-z_]+)-([0-9a-z]+)$/

let nextScenario: ScenarioKey | null = null
/** Submit page (mock mode) chooses which fixture the next POST /verify returns. */
export function setNextScenario(key: string | null): void {
  nextScenario = key && key in SCENARIOS ? (key as ScenarioKey) : null
}
export { SCENARIOS, SCENARIO_KEYS } from './scenarios'

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const meta = (started: number) => ({ request_id: `mock-req-${Math.random().toString(36).slice(2, 10)}`, latency_ms: Math.round(performance.now() - started) })

function notFound(path: string): ApiError {
  return new ApiError(404, `Not Found: no mock route for ${path}`, { title: 'Not Found', status: 404, detail: `No mock route for ${path}`, request_id: 'mock-req-404' })
}

function lookup(jobId: string): { scenario: Scenario; createdAt: number } {
  const m = ID_RE.exec(jobId)
  const scenario = m ? SCENARIOS[m[1] as ScenarioKey] : undefined
  if (!m || !scenario) {
    throw new ApiError(404, 'Job not found', { title: 'Job not found', status: 404, detail: `No mock job ${jobId}. Mock job ids look like mock-false-<time>.`, request_id: 'mock-req-404' })
  }
  const ts = parseInt(m[2]!, 36)
  return { scenario, createdAt: Number.isFinite(ts) && ts > 1_600_000_000_000 ? ts : Date.now() - 3_600_000 }
}

function totalMs(s: Scenario): number {
  return Math.max(...s.timeline.map(([, start, dur]) => start + dur))
}

/** JobView for a mock job at time `now` (queued -> processing with partial trace -> completed). */
export function materialize(jobId: string, now: number = Date.now()): JobView {
  const { scenario: s, createdAt } = lookup(jobId)
  const full = s.job
  const t0 = createdAt + QUEUE_MS
  const elapsed = now - t0
  const total = totalMs(s)
  const trace = s.timeline
    .filter(([, start]) => start <= elapsed)
    .map(([node, start, dur, status = 'ok', detail]) => {
      const done = start + dur <= elapsed
      return {
        node,
        started_at: iso(t0 + start),
        ended_at: done ? iso(t0 + start + dur) : null,
        duration_ms: done ? dur : null,
        status: done ? status : 'ok',
        detail: done ? (detail ?? null) : null,
      }
    })
  const base: JobView = { ...full, job_id: jobId, created_at: iso(createdAt), trace }

  if (now < t0) {
    return { ...emptyResult(base), status: 'queued', queue_position: 1, status_detail: 'queued (position 1)', trace: [] }
  }
  if (elapsed < total) {
    const ended = (n: string) => s.timeline.some(([node, start, dur]) => node === n && start + dur <= elapsed)
    const pd = s.processingDetail
    return {
      ...emptyResult(base),
      status: 'processing',
      input: ended('resolve_input') ? full.input : null,
      claim: ended('extract_claim') ? full.claim : null,
      status_detail: pd && elapsed >= pd.from && elapsed < pd.to ? pd.detail : null,
    }
  }
  return { ...base, status: 'completed', updated_at: iso(t0 + total) }
}

function emptyResult(v: JobView): JobView {
  return {
    ...v,
    verdict: null, confidence: null, abstain_reason: null, abstain_detail: null, evidence_state: null,
    rationale: [], cited_evidence_ids: [], independent_source_count: 0, sources: [], forensic: null, provenance: null,
    claim: null, input: null, fusion: null, debate: null, tool_errors: [], degraded_tools: [], labels: [],
    report_eligible: false, legal_eligible: false, legal_block_reason: null, audit_id: null, updated_at: null,
  }
}

// ---- Health ---------------------------------------------------------------------------------

function health(): HealthView {
  const flag = mockFlag('health')
  const tool = (state: HealthView['tool_status'][string]['state'], configured = true, failures = 0, last_error: string | null = null) => ({
    configured, state, version: 'mock-1', calls: 12, failures, last_error,
  })
  return {
    status: flag === 'degraded' ? 'degraded' : 'ok',
    mode: flag === 'replay' ? 'replay' : 'live',
    version: '0.1.0-mock',
    resolved_models: flag === 'extractive' ? { primary: 'none' } : MOCK_MODELS,
    llm_provider: flag === 'extractive' ? 'none' : 'aicredits',
    db: 'ok (mock)',
    ml: { embedder: 'loaded', nli: 'loaded', asr: 'loading', clip: 'loaded' },
    tool_status: {
      factcheck: tool('unconfigured', false),
      gnews_rss: tool('ok'),
      wire_rss: tool('ok'),
      pib_rss: tool('ok'),
      gdelt: flag === 'degraded' ? tool('open', true, 3, 'timeout') : tool('ok'),
      serper: tool('unconfigured', false),
      wikidata: tool('ok'),
      vision_web: tool('unconfigured', false),
      sightengine: tool('unconfigured', false),
      hf_inference: tool('unconfigured', false),
      llm_primary: flag === 'extractive' ? tool('disabled') : tool('ok'),
      llm_secondary: flag === 'extractive' ? tool('disabled') : tool('ok'),
      oembed: tool('ok'),
      page_fetch: tool('ok'),
      ytdlp: tool('ok'),
      playwright_fetch: tool('degraded', true, 1, 'browser not installed'),
    },
  }
}

// ---- Report / legal -------------------------------------------------------------------------

function reportPackage(v: JobView): ReportDraftPackage {
  if (!v.report_eligible || !v.verdict) {
    return { job_id: v.job_id, eligible: false, ineligible_reason: 'Reports are only prepared for FALSE or MANIPULATED verdicts.', preview_text: '', bundle: null, bundle_url: null, platform_plans: [], audit_id: null }
  }
  const cited = v.sources.filter((s) => v.cited_evidence_ids.includes(s.id))
  const claimText = v.claim?.claims[0]?.text ?? ''
  const lines = [
    `VeriLoop verification: ${v.verdict} (calibrated confidence ${Math.round((v.confidence ?? 0) * 100)}%)`,
    `Claim: "${claimText}"`,
    v.input?.url ? `Content: ${v.input.url}` : 'Content: uploaded file (hashes in bundle)',
    `Independent sources: ${v.independent_source_count}`,
    ...cited.map((s) => `  [${s.id}] ${s.source_name}: ${s.url}`),
    `Signed audit record #${v.audit_id}: /audit/${v.job_id}/pdf (verify: /audit/${v.job_id}/verify)`,
    'Prepared by VeriLoop. Submitted by a person, never automatically.',
  ]
  const preview = lines.join('\n')
  const platform = v.input?.platform ?? 'web'
  const plans: PlatformPlan[] = []
  if (['x', 'instagram', 'facebook', 'youtube', 'tiktok'].includes(platform)) {
    plans.push({
      platform,
      target_url: v.input?.url ?? null,
      method: 'playwright_guided',
      status: 'prepared',
      evidence_attached: true,
      report_category: v.verdict === 'MANIPULATED' ? 'Manipulated or synthetic media' : 'False information',
      prefilled_text: preview,
      card_url: null,
      steps: [
        `The reporter agent opens the ${PLATFORM_LABELS[platform]} report form for this post in a browser on this laptop.`,
        `It selects "${v.verdict === 'MANIPULATED' ? 'Manipulated or synthetic media' : 'False information'}" and pastes the text above.`,
        'It stops before the final step. You review everything and press Submit yourself.',
      ],
    })
  }
  for (const p of ['whatsapp', 'telegram'] as const) {
    plans.push({
      platform: p,
      target_url: null,
      method: 'report_card',
      status: 'prepared',
      evidence_attached: false,
      report_category: null,
      prefilled_text: preview,
      card_url: null,
      steps:
        p === 'whatsapp'
          ? ['Download the report card image.', 'Reply to the forwarded message in the same chat with the card and the text above.', 'Optionally report the sender in WhatsApp (Contact info → Report).']
          : ['Download the report card image.', 'Post it as a reply in the channel or group, with the text above.', 'Telegram has no report API: VeriLoop only prepares the card.'],
    })
  }
  return {
    job_id: v.job_id,
    eligible: true,
    ineligible_reason: null,
    preview_text: preview,
    bundle: {
      verdict: { label: v.verdict, confidence: v.confidence, evidence_state: v.evidence_state, rationale: v.rationale },
      sources: cited.map((s) => ({ id: s.id, source_name: s.source_name, url: s.url, published_at: s.published_at, retrieval_method: s.retrieval_method, stance: s.stance })),
      forensic_report: v.forensic && v.forensic.status !== 'unavailable' ? { status: v.forensic.status, ensemble_score: v.forensic.ensemble_score, models: v.forensic.models.map((m) => ({ model_id: m.model_id, raw_score: m.raw_score })) } : null,
      media_hashes: (v.input?.media_hashes ?? []).map((h) => ({ ...h })),
      timestamps: { job_created_at: v.created_at, verdict_at: v.updated_at ?? v.created_at, report_prepared_at: iso(Date.now()) },
      audit_id: v.audit_id,
      pdf_url: `/audit/${v.job_id}/pdf`,
      verify_url: `/audit/${v.job_id}/verify`,
    },
    bundle_url: `/report/${v.job_id}/bundle.zip`,
    platform_plans: plans,
    audit_id: (v.audit_id ?? 0) + 1000,
  }
}

function legalDraft(v: JobView, req: LegalDraftRequest): LegalDraftResponse {
  const name = v.claim?.subject.name ?? '[NAME OF PERSON DEPICTED]'
  const url = v.input?.url ?? '[URL OF THE CONTENT]'
  const ref = `VeriLoop evidence bundle ${v.job_id} (audit #${v.audit_id}), signed PDF: /audit/${v.job_id}/pdf`
  const common = {
    disclaimer: 'MOCK TEMPLATE. This is a draft generated from a template. It is not legal advice. VeriLoop never sends it.',
    evidence_bundle_ref: ref,
    audit_id: (v.audit_id ?? 0) + 2000,
  }
  const drafts: Record<Jurisdiction, Omit<LegalDraftResponse, keyof typeof common>> = {
    IN: {
      jurisdiction: 'IN',
      template_version: 'in-it-rules-2021+dpdp-2023/mock-1',
      placeholders: ['[YOUR FULL NAME]', '[YOUR CONTACT EMAIL]', '[GRIEVANCE OFFICER NAME]', '[DATE]'],
      draft_text: [
        'To: [GRIEVANCE OFFICER NAME], Grievance Officer, ' + PLATFORM_LABELS[v.input?.platform ?? 'web'],
        'Date: [DATE]',
        '',
        'Subject: Complaint regarding impersonation / artificially morphed content (IT Rules 2021, Rule 3(2)(b)) and request for erasure of personal data (DPDP Act 2023)',
        '',
        `I, [YOUR FULL NAME], am ${name}, the person depicted (or authorised to act on their behalf).`,
        `The content at ${url} is an artificially altered video that impersonates me.`,
        'Independent verification (attached) found it MANIPULATED, with the evidence listed below.',
        '',
        `Evidence: ${ref}`,
        '',
        'I request that you remove or disable access to this content as required under the IT Rules, 2021, and erase my personal data processed in it.',
        '',
        'Contact: [YOUR CONTACT EMAIL]',
      ].join('\n'),
      checklist: ['Fill every [PLACEHOLDER]', 'Attach the evidence bundle ZIP and signed PDF', 'Keep the complaint acknowledgement number', 'Escalate to the Grievance Appellate Committee if there is no response'],
      filing_instructions: ['Use the platform’s grievance officer contact (published under the IT Rules).', 'You may also report at the national cyber crime reporting portal.'],
    },
    EU: {
      jurisdiction: 'EU',
      template_version: 'eu-gdpr-art17/mock-1',
      placeholders: ['[YOUR FULL NAME]', '[YOUR ADDRESS]', '[DATA PROTECTION OFFICER]', '[DATE]'],
      draft_text: [
        'To: [DATA PROTECTION OFFICER]',
        'Date: [DATE]',
        '',
        'Subject: Request for erasure under Article 17 GDPR',
        '',
        `I, [YOUR FULL NAME], of [YOUR ADDRESS], request erasure of personal data relating to me contained in the content at ${url}.`,
        'The content is a manipulated depiction of me; there is no lawful basis for its processing.',
        `Evidence: ${ref}`,
        '',
        'Please confirm erasure within one month as required by Article 12(3) GDPR.',
      ].join('\n'),
      checklist: ['Fill every [PLACEHOLDER]', 'Attach proof of identity if requested', 'Note the one-month response deadline', 'You may complain to your national data protection authority'],
      filing_instructions: ['Send to the platform’s DPO or privacy contact form.'],
    },
    US: {
      jurisdiction: 'US',
      template_version: 'us-dmca-512c+ncii-note/mock-1',
      placeholders: ['[YOUR FULL NAME]', '[YOUR ADDRESS]', '[DESCRIPTION OF YOUR ORIGINAL WORK]', '[SIGNATURE]', '[DATE]'],
      draft_text: [
        'To: Designated DMCA Agent',
        'Date: [DATE]',
        '',
        'Subject: Notice of infringement (17 U.S.C. § 512(c)(3))',
        '',
        'Original work: [DESCRIPTION OF YOUR ORIGINAL WORK]',
        `Infringing material: ${url}`,
        'I have a good faith belief that the use is not authorized. The information in this notice is accurate, and under penalty of perjury, I am authorized to act on behalf of the owner.',
        `Supporting evidence: ${ref}`,
        '',
        '[SIGNATURE]',
        '[YOUR FULL NAME], [YOUR ADDRESS]',
        '',
        'Note: DMCA applies only if you own copyright in the source material. Many states also have statutes on non-consensual or deceptive synthetic media; check your state.',
      ].join('\n'),
      checklist: ['Confirm you own the copyright before using DMCA', 'Fill every [PLACEHOLDER]', 'Keep a copy of the notice', 'Consider your state’s synthetic-media / NCII statute'],
      filing_instructions: ['Send to the platform’s designated DMCA agent (listed with the US Copyright Office).'],
    },
  }
  return { ...drafts[req.jurisdiction], ...common }
}

// ---- Audit (real hashing + Ed25519 signing in the browser, so client-side checks work) -------

let keyPromise: Promise<CryptoKeyPair | null> | null = null
function keyPair(): Promise<CryptoKeyPair | null> {
  keyPromise ??= crypto.subtle
    .generateKey({ name: 'Ed25519' }, true, ['sign', 'verify'])
    .then((k) => k as CryptoKeyPair)
    .catch(() => null)
  return keyPromise
}

const hex = (buf: ArrayBuffer) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('')
const b64 = (buf: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(buf)))

function canonical(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  const obj = value as Record<string, unknown>
  return `{${Object.keys(obj)
    .sort()
    .filter((k) => obj[k] !== undefined)
    .map((k) => `${JSON.stringify(k)}:${canonical(obj[k])}`)
    .join(',')}}`
}

export async function mockPublicKeyPem(): Promise<string> {
  const kp = await keyPair()
  if (!kp) return '-----BEGIN PUBLIC KEY-----\nMOCK-KEY-UNAVAILABLE-IN-THIS-BROWSER\n-----END PUBLIC KEY-----'
  const spki = await crypto.subtle.exportKey('spki', kp.publicKey)
  const body = b64(spki).replace(/(.{64})/g, '$1\n').trim()
  return `-----BEGIN PUBLIC KEY-----\n${body}\n-----END PUBLIC KEY-----`
}

async function auditEntry(v: JobView): Promise<AuditEntryView> {
  const payload = {
    job_id: v.job_id,
    event_type: 'verdict',
    verdict: { label: v.verdict, confidence: v.confidence, abstain_reason: v.abstain_reason, evidence_state: v.evidence_state },
    evidence: v.sources.map((s) => ({ id: s.id, url: s.url, domain: s.domain, published_at: s.published_at, retrieval_method: s.retrieval_method })),
    media_hashes: v.input?.media_hashes ?? [],
    weights: Object.fromEntries((v.fusion?.signals ?? []).map((s) => [s.name, s.weight])),
    thresholds: v.fusion?.thresholds ?? {},
    calibration_version: v.fusion?.calibration_version ?? '',
    resolved_model_ids: v.resolved_models,
    trace: v.trace.map((t) => ({ node: t.node, ms: t.duration_ms, status: t.status })),
    mode: 'mock',
  }
  const payloadJson = canonical(payload)
  const payloadHash = hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(payloadJson)))
  const prevHash = hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`mock-prev-${(v.audit_id ?? 1) - 1}`)))
  const kp = await keyPair()
  let signature = 'MOCK-SIGNATURE (Ed25519 unavailable in this browser)'
  let fingerprint = 'mock-key'
  if (kp) {
    signature = b64(await crypto.subtle.sign({ name: 'Ed25519' }, kp.privateKey, new TextEncoder().encode(`${prevHash}:${payloadHash}`)))
    const raw = await crypto.subtle.exportKey('raw', kp.publicKey)
    fingerprint = `SHA256:${hex(await crypto.subtle.digest('SHA-256', raw)).slice(0, 32)}`
  }
  return {
    id: v.audit_id ?? 0,
    job_id: v.job_id,
    event_type: 'verdict',
    created_at: v.updated_at ?? v.created_at,
    prev_hash: prevHash,
    payload_hash: payloadHash,
    payload_json: payloadJson,
    resolved_model_ids: v.resolved_models,
    tool_versions: { gnews_rss: 'mock-1', wire_rss: 'mock-1', pib_rss: 'mock-1', gdelt: 'mock-1', wikidata: 'mock-1', phash_index: 'mock-1' },
    signature,
    key_fingerprint: fingerprint,
  }
}

// ---- Router ---------------------------------------------------------------------------------

function completedJob(jobId: string): JobView {
  const v = materialize(jobId)
  if (v.status !== 'completed') {
    throw new ApiError(409, 'Job still running', { title: 'Conflict', status: 409, detail: 'The job has not finished yet.', request_id: 'mock-req-409' })
  }
  return v
}

export async function handleRequest(method: string, path: string, body?: unknown): Promise<unknown> {
  const started = performance.now()
  await sleep(120 + Math.random() * 250)
  const p = path.split('?')[0] ?? path
  let m: RegExpExecArray | null

  if (method === 'GET' && p === '/health') return { ...health(), ...meta(started) }

  if (method === 'POST' && p === '/verify') {
    const req = (body ?? {}) as VerifyRequest
    const key = nextScenario ?? guessScenario(`${req.url ?? ''} ${req.text ?? ''}`)
    nextScenario = null
    return accepted(key, started)
  }
  if (method === 'GET' && (m = /^\/verify\/([^/]+)$/.exec(p))) return { ...materialize(decodeURIComponent(m[1]!)), ...meta(started) }

  if (method === 'POST' && (m = /^\/report\/([^/]+)$/.exec(p))) return { ...reportPackage(completedJob(decodeURIComponent(m[1]!))), ...meta(started) }
  if (method === 'POST' && (m = /^\/report\/([^/]+)\/confirm$/.exec(p))) {
    const v = completedJob(decodeURIComponent(m[1]!))
    const req = (body ?? {}) as ReportConfirmRequest
    if (req.confirmed !== true) throw new ApiError(422, 'Confirmation required', { title: 'Unprocessable Entity', status: 422, detail: 'confirmed must be true', request_id: 'mock-req-422' })
    const plan = reportPackage(v).platform_plans.find((pl) => pl.platform === req.platform)
    if (!plan) throw notFound(`${p} (platform ${req.platform})`)
    const res: ReportConfirmResponse = {
      attempt_logged: true,
      attempt_id: `att_mock_${Math.random().toString(36).slice(2, 10)}`,
      platform: req.platform,
      method: plan.method,
      reporter_agent_command: plan.method === 'playwright_guided' ? `python -m reporter_agent --job ${v.job_id} --platform ${req.platform}` : null,
      audit_id: (v.audit_id ?? 0) + 3000,
    }
    return { ...res, ...meta(started) }
  }
  if (method === 'POST' && (m = /^\/legal\/([^/]+)\/draft$/.exec(p))) {
    const v = completedJob(decodeURIComponent(m[1]!))
    const req = body as LegalDraftRequest
    if (!v.legal_eligible) throw new ApiError(403, 'Legal draft not available', { title: 'Forbidden', status: 403, detail: v.legal_block_reason ?? 'Not eligible', request_id: 'mock-req-403' })
    const a = req?.acknowledgements
    if (!a || !a.is_subject_or_authorized || !a.not_legal_advice || !a.no_auto_send || !a.will_review_before_filing) {
      throw new ApiError(422, 'All acknowledgements are required', { title: 'Unprocessable Entity', status: 422, detail: 'All acknowledgements are required', request_id: 'mock-req-422' })
    }
    return { ...legalDraft(v, req), ...meta(started) }
  }
  if (method === 'GET' && (m = /^\/audit\/([^/]+)\/verify$/.exec(p))) {
    const v = completedJob(decodeURIComponent(m[1]!))
    const tampered = mockFlag('tamper') === '1'
    const res: AuditVerifyResult = {
      ok: !tampered,
      checked_rows: 240 + (v.audit_id ?? 0) % 50,
      broken_at: tampered ? 37 : null,
      reason: tampered ? 'payload_hash mismatch: row 37 payload_json was modified after signing' : null,
      head_hash: (await auditEntry(v)).payload_hash,
      job_entry_ids: [v.audit_id ?? 0],
    }
    return { ...res, ...meta(started) }
  }
  if (method === 'GET' && (m = /^\/audit\/([^/]+)$/.exec(p)) && m[1] !== 'pubkey') {
    return { ...(await auditEntry(completedJob(decodeURIComponent(m[1]!)))), ...meta(started) }
  }
  throw notFound(`${method} ${p}`)
}

function accepted(key: ScenarioKey, started: number): VerifyAccepted & { request_id: string; latency_ms: number } {
  const jobId = `mock-${key}-${Date.now().toString(36)}`
  return { job_id: jobId, status: 'queued', status_url: `/verify/${jobId}`, events_url: `/verify/${jobId}/events`, queue_position: 0, ...meta(started) }
}

export async function mockUpload(
  file: File,
  fields: Omit<VerifyRequest, 'upload_id'>,
  onProgress?: (fraction: number) => void,
): Promise<VerifyAccepted> {
  const started = performance.now()
  for (let i = 1; i <= 10; i++) {
    await sleep(60)
    onProgress?.(i / 10)
  }
  const key = nextScenario ?? (file.type.startsWith('video/') ? 'manipulated' : guessScenario(`${file.name} ${fields.text ?? ''}`))
  nextScenario = null
  return accepted(key, started)
}

// ---- Simulated SSE --------------------------------------------------------------------------

export function mockSubscribe(jobId: string, h: JobEventHandlers): () => void {
  let s: Scenario
  let createdAt: number
  try {
    ;({ scenario: s, createdAt } = lookup(jobId))
  } catch {
    queueMicrotask(h.onClosed)
    return () => {}
  }
  const t0 = createdAt + QUEUE_MS
  const total = totalMs(s)
  const schedule: Array<{ at: number; event: JobEvent }> = [
    { at: createdAt, event: { type: 'status', status: 'queued', queue_position: 1 } },
    { at: t0, event: { type: 'status', status: 'processing' } },
  ]
  for (const [node, start, dur, status = 'ok', detail] of s.timeline) {
    schedule.push({ at: t0 + start, event: { type: 'node', node, status: 'start' } })
    schedule.push({
      at: t0 + start + dur,
      event: { type: 'node', node, status: status === 'error' ? 'error' : 'end', duration_ms: dur, ...(detail || status === 'skipped' ? { detail: detail ?? 'skipped' } : {}) },
    })
  }
  if (s.processingDetail) {
    schedule.push({ at: t0 + s.processingDetail.from, event: { type: 'status', status: 'processing', status_detail: s.processingDetail.detail } })
    schedule.push({ at: t0 + s.processingDetail.to, event: { type: 'status', status: 'processing', status_detail: null } })
  }
  for (const e of s.extraEvents ?? []) schedule.push({ at: t0 + e.at, event: e.event })
  schedule.push({ at: t0 + total + 40, event: { type: 'done', status: 'completed', verdict: s.job.verdict } })
  schedule.sort((a, b) => a.at - b.at)

  const timers: ReturnType<typeof setTimeout>[] = []
  const now = Date.now()
  const failAt = mockFlag('sse') === 'fail' ? now + 1500 : Infinity
  let closed = false
  timers.push(setTimeout(() => h.onOpen?.(), 0))
  for (const item of schedule) {
    if (item.at > failAt) break
    timers.push(setTimeout(() => !closed && h.onEvent(item.event), Math.max(0, item.at - now)))
  }
  if (Number.isFinite(failAt)) {
    timers.push(setTimeout(() => { if (!closed) { closed = true; h.onClosed() } }, failAt - now))
  } else {
    timers.push(setTimeout(() => { if (!closed) { closed = true; h.onClosed() } }, Math.max(0, t0 + total + 400 - now)))
  }
  return () => {
    closed = true
    timers.forEach(clearTimeout)
  }
}
