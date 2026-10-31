import { describe, expect, it } from 'vitest'
import type { NodeTiming } from '../api/types'
import { applyEvent, emptyLiveTrace, isTerminalEvent, liveToTimings, mergeTimings, modelFromLive, modelFromTimings, parseEventData, tickStep } from './trace'

describe('SSE event parsing', () => {
  it('parses documented node events', () => {
    expect(parseEventData('{"type":"node","node":"judge","status":"end","duration_ms":3100}')).toEqual({ type: 'node', node: 'judge', status: 'end', duration_ms: 3100 })
  })
  it('uses the SSE event name when the payload has no type', () => {
    expect(parseEventData('{"status":"processing"}', 'status')).toEqual({ type: 'status', status: 'processing' })
  })
  it('never throws on malformed data', () => {
    expect(parseEventData('not json', 'log')).toEqual({ type: 'log', raw: 'not json' })
    expect(parseEventData('[1,2]')).toEqual({ type: 'message', value: [1, 2] })
    expect(parseEventData('   ')).toBeNull()
  })
  it('recognizes terminal events', () => {
    expect(isTerminalEvent({ type: 'done' })).toBe(true)
    expect(isTerminalEvent({ type: 'status', status: 'completed' })).toBe(true)
    expect(isTerminalEvent({ type: 'node', node: 'audit_write', status: 'end' })).toBe(true)
    expect(isTerminalEvent({ type: 'node', node: 'audit_write', status: 'start' })).toBe(false)
    expect(isTerminalEvent({ type: 'status', status: 'processing' })).toBe(false)
  })
})

describe('live trace', () => {
  it('builds running and finished rows with the three parallel branches', () => {
    let s = emptyLiveTrace()
    s = applyEvent(s, { type: 'node', node: 'resolve_input', status: 'start' }, 1000)
    s = applyEvent(s, { type: 'node', node: 'resolve_input', status: 'end', duration_ms: 500 }, 1500)
    s = applyEvent(s, { type: 'node', node: 'evidence_branch', status: 'start' }, 1600)
    s = applyEvent(s, { type: 'node', node: 'forensic_branch', status: 'start' }, 1601)
    s = applyEvent(s, { type: 'node', node: 'provenance_branch', status: 'start' }, 1602)
    s = applyEvent(s, { type: 'node', node: 'provenance_branch', status: 'error', duration_ms: 300 }, 1902)
    s = applyEvent(s, { type: 'status', status: 'processing', status_detail: 'processing (rate-limited: serper)' }, 1950)
    const m = modelFromLive(s, 2600, false)
    const row = (n: string) => m.rows.find((r) => r.node === n)!
    expect(row('resolve_input')).toMatchObject({ status: 'ok', start: 0, end: 500, durationMs: 500 })
    expect(row('evidence_branch')).toMatchObject({ status: 'running', start: 600, parallel: true })
    expect(row('evidence_branch').end).toBe(1600) // grows to "now"
    expect(row('provenance_branch')).toMatchObject({ status: 'error', parallel: true })
    expect(row('judge').status).toBe('pending')
    expect(m.rows.filter((r) => r.parallel).map((r) => r.node)).toEqual(['evidence_branch', 'forensic_branch', 'provenance_branch'])
    expect(s.others).toHaveLength(1)
  })

  it('back-dates a node whose start was missed (connected mid-run)', () => {
    const s = applyEvent(emptyLiveTrace(), { type: 'node', node: 'judge', status: 'end', duration_ms: 400 }, 5000)
    const r = modelFromLive(s, 5000, false).rows.find((x) => x.node === 'judge')!
    expect(r.durationMs).toBe(400)
    expect((r.end ?? 0) - (r.start ?? 0)).toBe(400)
  })

  it('keeps unknown node names as extra rows', () => {
    const s = applyEvent(emptyLiveTrace(), { type: 'node', node: 'resolve_input', status: 'start' }, 0)
    const s2 = applyEvent(s, { type: 'node', node: 'ocr_subtask', status: 'start' }, 10)
    expect(modelFromLive(s2, 20, false).rows.at(-1)?.node).toBe('ocr_subtask')
  })
})

describe('server trace (NodeTiming[])', () => {
  const t = (node: string, s: number, d: number | null, status = 'ok'): NodeTiming => ({
    node,
    started_at: new Date(Date.UTC(2026, 9, 31, 8, 0, 0) + s).toISOString(),
    ended_at: d == null ? null : new Date(Date.UTC(2026, 9, 31, 8, 0, 0) + s + d).toISOString(),
    duration_ms: d,
    status,
    detail: null,
  })

  it('computes offsets from the earliest start and marks unrun nodes as skipped when finished', () => {
    const m = modelFromTimings([t('resolve_input', 0, 600), t('extract_claim', 650, 1000), t('check_worthiness', 1700, 100), t('decide_action', 1820, 10), t('audit_write', 1840, 100)], true)
    const row = (n: string) => m.rows.find((r) => r.node === n)!
    expect(row('extract_claim')).toMatchObject({ start: 650, end: 1650, status: 'ok' })
    expect(row('evidence_branch')).toMatchObject({ status: 'skipped', start: null })
    expect(m.totalMs).toBe(1940)
  })

  it('shows a node without end as running while the job is live', () => {
    const m = modelFromTimings([t('resolve_input', 0, 500), t('evidence_branch', 600, null)], false, Date.UTC(2026, 9, 31, 8, 0, 2))
    expect(m.rows.find((r) => r.node === 'evidence_branch')).toMatchObject({ status: 'running', end: 2000 })
  })

  it('accepts alias node names', () => {
    const m = modelFromTimings([t('evidence', 0, 100), t('forensic', 0, 120)], true)
    expect(m.rows.find((r) => r.node === 'evidence_branch')?.status).toBe('ok')
    expect(m.rows.find((r) => r.node === 'forensic_branch')?.status).toBe('ok')
  })
})

describe('tickStep', () => {
  it('picks round steps', () => {
    expect(tickStep(9000)).toBe(2000)
    expect(tickStep(1000)).toBe(200)
    expect(tickStep(14000)).toBe(5000)
  })
})

describe('merging server and live timings (reconnect mid-run)', () => {
  it('prefers finished server spans and keeps live-only running nodes', () => {
    const base = Date.UTC(2026, 9, 31, 8, 0, 0)
    const server = [
      { node: 'resolve_input', started_at: new Date(base).toISOString(), ended_at: new Date(base + 600).toISOString(), duration_ms: 600, status: 'ok', detail: null },
      { node: 'extract_claim', started_at: new Date(base + 650).toISOString(), ended_at: null, duration_ms: null, status: 'ok', detail: null },
    ]
    // burst replay: everything received at ~the same client time
    let s = emptyLiveTrace()
    s = applyEvent(s, { type: 'node', node: 'resolve_input', status: 'start' }, 1000)
    s = applyEvent(s, { type: 'node', node: 'resolve_input', status: 'end', duration_ms: 600 }, 1001)
    s = applyEvent(s, { type: 'node', node: 'extract_claim', status: 'start' }, 1002)
    s = applyEvent(s, { type: 'node', node: 'extract_claim', status: 'end', duration_ms: 1400 }, 1003)
    s = applyEvent(s, { type: 'node', node: 'evidence_branch', status: 'start' }, 1004)
    const merged = mergeTimings(server, liveToTimings(s, base + 2050 - 1004))
    const m = modelFromTimings(merged, false, base + 3000)
    const row = (n: string) => m.rows.find((r) => r.node === n)!
    expect(row('resolve_input')).toMatchObject({ start: 0, end: 600 })
    expect(row('extract_claim')).toMatchObject({ start: 650, end: 2050, status: 'ok' })
    expect(row('evidence_branch').status).toBe('running')
  })

  it('marks nodes that end with a "skipped" detail as skipped', () => {
    let s = applyEvent(emptyLiveTrace(), { type: 'node', node: 'judge', status: 'start' }, 0)
    s = applyEvent(s, { type: 'node', node: 'judge', status: 'end', duration_ms: 5, detail: 'skipped: fusion abstained' }, 5)
    expect(modelFromLive(s, 10, false).rows.find((r) => r.node === 'judge')?.status).toBe('skipped')
  })
})
