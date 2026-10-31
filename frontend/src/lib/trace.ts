import type { GenericJobEvent, JobEvent, NodeEvent, NodeTiming } from '../api/types'

/** Orchestrator graph nodes in execution order (BUILD_SPEC §6). */
export const GRAPH_NODES = [
  'resolve_input',
  'extract_claim',
  'check_worthiness',
  'evidence_branch',
  'forensic_branch',
  'provenance_branch',
  'fuse_evidence',
  'judge',
  'decide_action',
  'build_report',
  'audit_write',
] as const

export const PARALLEL_NODES: ReadonlySet<string> = new Set(['evidence_branch', 'forensic_branch', 'provenance_branch'])

export const NODE_INFO: Record<string, { label: string; hint: string }> = {
  resolve_input: { label: 'Resolve input', hint: 'Normalize the link, detect the platform, fetch media and metadata' },
  extract_claim: { label: 'Extract claim', hint: 'Speech-to-text, on-screen text, caption; split into claims' },
  check_worthiness: { label: 'Check-worthiness', hint: 'Cheap gate: is there a factual claim worth checking?' },
  evidence_branch: { label: 'Evidence', hint: 'Fact-check registry, news wires, GDELT, PIB, Wikidata' },
  forensic_branch: { label: 'Forensics', hint: 'Deepfake detector ensemble, EXIF and C2PA metadata' },
  provenance_branch: { label: 'Provenance', hint: 'pHash/CLIP index, reverse image search, first-seen' },
  fuse_evidence: { label: 'Fuse evidence', hint: 'Weighted aggregation and calibration' },
  judge: { label: 'Debate & judge', hint: 'Proponent / opponent / judge, then verifier and second judge' },
  decide_action: { label: 'Decide', hint: 'Verdict or abstain; report and legal eligibility' },
  build_report: { label: 'Evidence bundle', hint: 'Assemble the report package (not sent)' },
  audit_write: { label: 'Audit record', hint: 'Hash-chained, signed audit entry' },
}

/** Tolerate small naming differences between the spec and the backend. */
const NODE_ALIASES: Record<string, string> = {
  resolve: 'resolve_input',
  extract: 'extract_claim',
  extract_claims: 'extract_claim',
  worthiness: 'check_worthiness',
  evidence: 'evidence_branch',
  forensic: 'forensic_branch',
  forensics: 'forensic_branch',
  provenance: 'provenance_branch',
  fuse: 'fuse_evidence',
  fusion: 'fuse_evidence',
  debate: 'judge',
  decide: 'decide_action',
  report: 'build_report',
  audit: 'audit_write',
}

export function canonicalNode(name: string): string {
  return NODE_ALIASES[name] ?? name
}

export function nodeLabel(name: string): string {
  return NODE_INFO[name]?.label ?? name.replace(/_/g, ' ')
}

export type RowStatus = 'pending' | 'running' | 'ok' | 'error' | 'skipped'

export interface TraceRow {
  node: string
  status: RowStatus
  /** ms from the start of the run */
  start: number | null
  end: number | null
  /** server-reported duration when known */
  durationMs: number | null
  detail: string | null
  parallel: boolean
  known: boolean
}

export interface TraceModel {
  rows: TraceRow[]
  /** time axis length in ms */
  totalMs: number
}

// ---- SSE event parsing ----------------------------------------------------------------------

export function isNodeEvent(ev: JobEvent): ev is NodeEvent {
  return ev.type === 'node' && typeof (ev as NodeEvent).node === 'string'
}

/** Parse one SSE `data:` payload. Never throws: malformed data becomes a generic event. */
export function parseEventData(data: string, eventName?: string): JobEvent | null {
  const fallbackType = eventName && eventName !== 'message' ? eventName : 'message'
  if (!data || !data.trim()) return null
  try {
    const parsed: unknown = JSON.parse(data)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const obj = parsed as Record<string, unknown>
      const type = typeof obj.type === 'string' && obj.type ? obj.type : fallbackType
      return { ...obj, type } as JobEvent
    }
    return { type: fallbackType, value: parsed }
  } catch {
    return { type: fallbackType, raw: data }
  }
}

const TERMINAL_TYPES = new Set(['done', 'complete', 'completed', 'finished', 'end', 'job_completed', 'job_done', 'verdict', 'result'])

/** An event after which the job view should be re-fetched (the run is over or about to be). */
export function isTerminalEvent(ev: JobEvent): boolean {
  if (isNodeEvent(ev)) return canonicalNode(ev.node) === 'audit_write' && ev.status !== 'start'
  if (TERMINAL_TYPES.has(ev.type)) return true
  const st = (ev as GenericJobEvent).status
  return st === 'completed' || st === 'failed'
}

// ---- Live trace (from SSE, client receive times) --------------------------------------------

interface LiveNode {
  start: number | null
  end: number | null
  status: RowStatus
  durationMs: number | null
  detail: string | null
}

export interface LiveTrace {
  /** client time (ms) of the first event */
  t0: number | null
  nodes: Record<string, LiveNode>
  order: string[]
  /** non-node events, newest last (capped) */
  others: Array<{ at: number; event: JobEvent }>
}

export function emptyLiveTrace(): LiveTrace {
  return { t0: null, nodes: {}, order: [], others: [] }
}

const MAX_OTHER_EVENTS = 50

export function applyEvent(state: LiveTrace, ev: JobEvent, at: number): LiveTrace {
  const t0 = state.t0 ?? at
  if (!isNodeEvent(ev)) {
    return { ...state, t0, others: [...state.others, { at: at - t0, event: ev }].slice(-MAX_OTHER_EVENTS) }
  }
  const node = canonicalNode(ev.node)
  const rel = at - t0
  const prev = state.nodes[node]
  const duration = typeof ev.duration_ms === 'number' && Number.isFinite(ev.duration_ms) ? ev.duration_ms : null
  const detail = typeof ev.detail === 'string' ? ev.detail : (prev?.detail ?? null)
  let next: LiveNode
  if (ev.status === 'start') {
    next = { start: rel, end: null, status: 'running', durationMs: null, detail }
  } else {
    // end/error: if we connected mid-run and missed "start", back-date it from duration_ms.
    const start = prev?.start ?? (duration != null ? Math.max(0, rel - duration) : rel)
    const end = duration != null ? start + duration : rel
    const skipped = ev.skipped === true || (typeof detail === 'string' && /^skipped\b/i.test(detail))
    next = { start, end, status: ev.status === 'error' ? 'error' : skipped ? 'skipped' : 'ok', durationMs: duration ?? end - start, detail }
  }
  const order = state.order.includes(node) ? state.order : [...state.order, node]
  return { ...state, t0, nodes: { ...state.nodes, [node]: next }, order }
}

/**
 * Live SSE timings (client receive clock) as NodeTiming[] on the wall clock, so they can be merged
 * with the server's partial `JobView.trace`. `perfToWall` = Date.now() - performance.now().
 */
export function liveToTimings(state: LiveTrace, perfToWall: number): NodeTiming[] {
  if (state.t0 == null) return []
  const base = state.t0 + perfToWall
  const out: NodeTiming[] = []
  for (const node of state.order) {
    const n = state.nodes[node]
    if (!n || n.start == null) continue
    const running = n.status === 'running'
    out.push({
      node,
      started_at: new Date(base + n.start).toISOString(),
      ended_at: running || n.end == null ? null : new Date(base + n.end).toISOString(),
      duration_ms: running ? null : n.durationMs,
      status: n.status === 'error' ? 'error' : n.status === 'skipped' ? 'skipped' : 'ok',
      detail: n.detail,
    })
  }
  return out
}

/**
 * Merge server timings (accurate timestamps, may lag) with live ones (immediate, but a reconnect
 * replays past events in a burst). Per node: a finished server entry wins, then a finished live
 * entry, then the server's start time for a running node.
 */
export function mergeTimings(server: NodeTiming[], live: NodeTiming[]): NodeTiming[] {
  const byNode = new Map<string, NodeTiming>()
  for (const t of server) byNode.set(canonicalNode(t.node), { ...t, node: canonicalNode(t.node) })
  for (const t of live) {
    const s = byNode.get(t.node)
    if (!s) byNode.set(t.node, t)
    else if (!s.ended_at && t.ended_at) {
      const ended = t.duration_ms != null ? new Date(Date.parse(s.started_at) + t.duration_ms).toISOString() : t.ended_at
      byNode.set(t.node, { ...t, started_at: s.started_at, ended_at: ended })
    }
  }
  return [...byNode.values()]
}

// ---- Rows -----------------------------------------------------------------------------------

function orderRows(rows: Map<string, TraceRow>, seenOrder: string[], terminal: boolean): TraceRow[] {
  const anyKnownSeen = GRAPH_NODES.some((n) => rows.has(n))
  const out: TraceRow[] = []
  // Known graph nodes in order; placeholders only make sense if the backend uses these names.
  if (anyKnownSeen || rows.size === 0) {
    for (const n of GRAPH_NODES) {
      out.push(
        rows.get(n) ?? {
          node: n,
          status: terminal ? 'skipped' : 'pending',
          start: null,
          end: null,
          durationMs: null,
          detail: null,
          parallel: PARALLEL_NODES.has(n),
          known: true,
        },
      )
    }
  }
  for (const n of seenOrder) {
    if (!(GRAPH_NODES as readonly string[]).includes(n)) {
      const r = rows.get(n)
      if (r) out.push(r)
    }
  }
  return out
}

function totalOf(rows: TraceRow[]): number {
  let max = 0
  for (const r of rows) max = Math.max(max, r.end ?? 0, r.start ?? 0)
  return max
}

export function modelFromLive(state: LiveTrace, now: number, terminal: boolean): TraceModel {
  const rel = state.t0 == null ? 0 : now - state.t0
  const rows = new Map<string, TraceRow>()
  for (const n of state.order) {
    const ln = state.nodes[n]
    if (!ln) continue
    const running = ln.status === 'running'
    rows.set(n, {
      node: n,
      status: running && terminal ? 'ok' : ln.status,
      start: ln.start,
      end: running ? (terminal ? ln.start : Math.max(rel, ln.start ?? 0)) : ln.end,
      durationMs: running ? null : ln.durationMs,
      detail: ln.detail,
      parallel: PARALLEL_NODES.has(n) || n.includes('branch'),
      known: n in NODE_INFO,
    })
  }
  const list = orderRows(rows, state.order, terminal)
  return { rows: list, totalMs: Math.max(totalOf(list), terminal ? 0 : rel) }
}

/** Authoritative trace from GET /verify/{id} (`JobView.trace`, server timestamps). */
export function modelFromTimings(trace: NodeTiming[], terminal: boolean, now: number = Date.now()): TraceModel {
  const valid = trace.filter((t) => !Number.isNaN(Date.parse(t.started_at)))
  if (valid.length === 0) return { rows: orderRows(new Map(), [], terminal), totalMs: 0 }
  const t0 = Math.min(...valid.map((t) => Date.parse(t.started_at)))
  const rows = new Map<string, TraceRow>()
  const order: string[] = []
  for (const t of valid) {
    const node = canonicalNode(t.node)
    const start = Date.parse(t.started_at) - t0
    const endAbs = t.ended_at ? Date.parse(t.ended_at) : NaN
    let end: number | null = Number.isNaN(endAbs) ? null : endAbs - t0
    if (end == null && t.duration_ms != null) end = start + t.duration_ms
    const status: RowStatus =
      t.status === 'error' ? 'error' : t.status === 'skipped' ? 'skipped' : end == null && !terminal ? 'running' : 'ok'
    if (status === 'running') end = Math.max(start, now - t0)
    const prev = rows.get(node)
    // A node can appear twice (retry); merge into one span.
    const merged: TraceRow = prev
      ? {
          ...prev,
          start: Math.min(prev.start ?? start, start),
          end: Math.max(prev.end ?? 0, end ?? 0),
          status: prev.status === 'error' || status === 'error' ? 'error' : status,
          durationMs: (prev.durationMs ?? 0) + (t.duration_ms ?? 0),
          detail: t.detail ?? prev.detail,
        }
      : {
          node,
          status,
          start,
          end,
          durationMs: t.duration_ms ?? (end != null ? end - start : null),
          detail: t.detail,
          parallel: PARALLEL_NODES.has(node) || node.includes('branch'),
          known: node in NODE_INFO,
        }
    rows.set(node, merged)
    if (!order.includes(node)) order.push(node)
  }
  const list = orderRows(rows, order, terminal)
  return { rows: list, totalMs: totalOf(list) }
}

/** "Nice" tick step for a time axis of `total` ms with about `target` ticks. */
export function tickStep(total: number, target = 5): number {
  const raw = Math.max(total, 1) / target
  const pow = 10 ** Math.floor(Math.log10(raw))
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pow >= raw) return m * pow
  return 10 * pow
}
