import { describe, expect, it } from 'vitest'
import { allEvidence } from '../lib/evidence'
import { SCENARIOS, SCENARIO_KEYS } from './scenarios'
import { materialize } from './server'

/** Mock data must be obviously fake and must itself respect the guardrails the UI is showing off. */
describe('mock fixtures', () => {
  it.each(SCENARIO_KEYS)('%s: every URL is https://example.org/mock/..., every model id is mock/...', (key) => {
    const json = JSON.stringify(SCENARIOS[key].job)
    const urls = json.match(/https?:\/\/[^"\s]+/g) ?? []
    for (const u of urls) expect(u.startsWith('https://example.org/mock/')).toBe(true)
    for (const id of Object.values(SCENARIOS[key].job.resolved_models)) expect(id.startsWith('mock/')).toBe(true)
    for (const m of SCENARIOS[key].job.forensic?.models ?? []) expect(m.model_id.startsWith('mock/')).toBe(true)
  })

  it.each(SCENARIO_KEYS)('%s: cited ids exist, quotes are substrings, decisive verdicts have >= 2 groups', (key) => {
    const job = SCENARIOS[key].job
    const items = allEvidence(job)
    const ids = new Set(items.map((i) => i.id))
    for (const id of job.cited_evidence_ids) expect(ids.has(id)).toBe(true)
    for (const s of job.rationale) for (const id of s.evidence_ids) expect(ids.has(id)).toBe(true)
    for (const it of items) if (it.stance_quote) expect(it.snippet).toContain(it.stance_quote)
    if (job.verdict === 'TRUE' || job.verdict === 'FALSE' || job.verdict === 'MANIPULATED') {
      const groups = new Set(items.filter((i) => job.cited_evidence_ids.includes(i.id)).map((i) => i.publisher_group))
      expect(groups.size).toBeGreaterThanOrEqual(2)
      expect(job.independent_source_count).toBeGreaterThanOrEqual(2)
    }
  })

  it('covers every verdict state plus abstain, no-evidence and degraded cases', () => {
    const verdicts = new Set(SCENARIO_KEYS.map((k) => SCENARIOS[k].job.verdict))
    for (const v of ['TRUE', 'FALSE', 'MANIPULATED', 'UNVERIFIABLE', 'NOT_CHECKABLE']) expect(verdicts.has(v as never)).toBe(true)
    expect(SCENARIOS.no_evidence.job.evidence_state).toBe('no_evidence')
    expect(SCENARIOS.degraded.job.degraded_tools.length).toBeGreaterThan(0)
    expect(SCENARIOS.abstain.job.abstain_reason).toBeTruthy()
  })

  it('mock server walks queued -> processing (with partial trace) -> completed', () => {
    const created = Date.now()
    const id = `mock-degraded-${created.toString(36)}`
    expect(materialize(id, created + 100).status).toBe('queued')
    const mid = materialize(id, created + 700 + 3000)
    expect(mid.status).toBe('processing')
    expect(mid.verdict).toBeNull()
    expect(mid.status_detail).toBe('processing (rate-limited: gnews_rss)')
    expect(mid.trace.length).toBeGreaterThan(0)
    expect(mid.trace.some((t) => t.ended_at === null)).toBe(true)
    const done = materialize(id, created + 60_000)
    expect(done.status).toBe('completed')
    expect(done.verdict).toBe('TRUE')
    expect(done.job_id).toBe(id)
  })
})
