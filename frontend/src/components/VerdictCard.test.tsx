import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { SCENARIOS } from '../mocks/scenarios'
import { allEvidence } from '../lib/evidence'
import { EvidenceList } from './EvidenceList'
import { ForensicPanel } from './ForensicPanel'
import { VerdictCard } from './VerdictCard'

function render(key: keyof typeof SCENARIOS): string {
  const job = SCENARIOS[key].job
  const index = new Map(allEvidence(job).map((e) => [e.id, e]))
  return renderToStaticMarkup(<VerdictCard job={job} sourceIndex={index} />)
}

describe('VerdictCard rendering (G8)', () => {
  it('no-evidence card: dashed neutral border, search icon text, explicit NOT-false label, no red', () => {
    const html = render('no_evidence')
    expect(html).toContain('data-verdict-style="no_evidence"')
    expect(html).toContain('border-dashed')
    expect(html).toContain('border-neutral-line')
    expect(html).toContain('No evidence found')
    expect(html).toContain('this is NOT the same as false')
    expect(html).not.toMatch(/false-(solid|line|soft|fg)/)
    // no confidence meter for "nothing to weigh"
    expect(html).not.toContain('role="meter"')
  })

  it('refuted FALSE card: solid red header with independent source count and citation chips', () => {
    const html = render('false')
    expect(html).toContain('data-verdict-style="false"')
    expect(html).toContain('border-solid')
    expect(html).toContain('bg-false-solid')
    expect(html).toContain('Refuted by 4 independent sources')
    expect(html).toContain('href="#ev-E1"')
    expect(html).toContain('role="meter"')
  })

  it('every scenario renders a distinct style key', () => {
    const keys = (['true', 'false', 'manipulated', 'abstain', 'no_evidence', 'not_checkable'] as const).map((k) => /data-verdict-style="([^"]+)"/.exec(render(k))?.[1])
    expect(new Set(keys).size).toBe(6)
  })

  it('ungrounded rationale: failing sentences are highlighted with the verifier reason', () => {
    const html = render('ungrounded')
    expect(html).toContain('Rejected explanation')
    expect((html.match(/Not grounded:/g) ?? []).length).toBe(2)
    expect(html).toContain('E2 says 12%, not 15%')
  })

  it('degraded tools and caveat labels are shown (deduplicated)', () => {
    const html = render('degraded')
    expect((html.match(/GDELT unavailable — result degraded/g) ?? []).length).toBe(1)
    expect(html).toContain('Serper search unavailable — result degraded')
    expect(html).toContain('Second judge disagreed')
  })
})

describe('EvidenceList / ForensicPanel rendering', () => {
  it('no-evidence list shows the neutral G8 panel instead of an empty table', () => {
    const job = SCENARIOS.no_evidence.job
    const html = renderToStaticMarkup(<EvidenceList items={[]} citedIds={[]} evidenceState={job.evidence_state} independentCount={0} />)
    expect(html).toContain('data-evidence-state="no_evidence"')
    expect(html).toContain('No evidence found — this is NOT the same as false')
  })

  it('evidence rows have anchors, highlighted quotes, government flag and API-provided links only', () => {
    const job = SCENARIOS.true.job
    const html = renderToStaticMarkup(<EvidenceList items={job.sources} citedIds={job.cited_evidence_ids} evidenceState={job.evidence_state} independentCount={3} />)
    expect(html).toContain('id="ev-E3"')
    expect(html).toContain('<mark>shall be opened for public service from 1 November 2026</mark>')
    expect(html).toContain('Government source (PIB)')
    const hrefs = [...html.matchAll(/href="([^"]+)"/g)].map((m) => m[1])
    expect(hrefs.length).toBeGreaterThan(0)
    for (const h of hrefs) expect(job.sources.map((s) => s.url)).toContain(h)
  })

  it('forensic panel always states that missing C2PA is not evidence of manipulation', () => {
    const html = renderToStaticMarkup(<ForensicPanel forensic={SCENARIOS.manipulated.job.forensic} input={SCENARIOS.manipulated.job.input} />)
    expect(html).toContain('Absence of C2PA credentials is not evidence of manipulation.')
    expect(html).toContain('Weighted ensemble')
    expect(html).toContain('mock/xception-ffpp')
  })
})
