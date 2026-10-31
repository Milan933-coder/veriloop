import { describe, expect, it } from 'vitest'
import { EVIDENCE_STATES, VERDICT_LABELS, type EvidenceState, type VerdictLabel } from '../api/types'
import { EVIDENCE_STATE_COPY, NO_EVIDENCE_BADGE, findThreshold, showConfidence, verdictVisual, type VerdictInput } from './verdict'

const base = (p: Partial<VerdictInput>): VerdictInput => ({
  status: 'completed',
  verdict: null,
  evidence_state: null,
  abstain_reason: null,
  independent_source_count: 0,
  ...p,
})

describe('verdictVisual: five distinct verdict states', () => {
  it('maps every label to a distinct key, icon and border/fill combination', () => {
    const cases: Array<[VerdictLabel, EvidenceState]> = [
      ['TRUE', 'supports'],
      ['FALSE', 'refutes'],
      ['MANIPULATED', 'manipulated'],
      ['UNVERIFIABLE', 'insufficient'],
      ['NOT_CHECKABLE', 'not_checkable'],
    ]
    const visuals = cases.map(([verdict, evidence_state]) => verdictVisual(base({ verdict, evidence_state, independent_source_count: 3 })))
    expect(new Set(visuals.map((v) => v.key)).size).toBe(5)
    expect(new Set(visuals.map((v) => v.icon)).size).toBe(5)
    expect(new Set(visuals.map((v) => `${v.border}/${v.filled}/${v.tone}`)).size).toBe(5)
    // colour is never the only signal: every state has words
    for (const v of visuals) {
      expect(v.headline.length).toBeGreaterThan(0)
      expect(v.badge.length).toBeGreaterThan(0)
    }
  })

  it('covers every VerdictLabel without throwing', () => {
    for (const verdict of VERDICT_LABELS) {
      expect(verdictVisual(base({ verdict })).key).not.toBe('pending')
    }
  })

  it('FALSE with refutes is solid red and states the independent source count', () => {
    const v = verdictVisual(base({ verdict: 'FALSE', evidence_state: 'refutes', independent_source_count: 4 }))
    expect(v.key).toBe('false')
    expect(v.tone).toBe('false')
    expect(v.filled).toBe(true)
    expect(v.border).toBe('solid')
    expect(v.icon).toBe('x-octagon')
    expect(v.summary).toContain('4 independent sources')
    expect(v.badge).toContain('refuted by 4 independent sources')
  })

  it('uses singular "source" for one source', () => {
    expect(verdictVisual(base({ verdict: 'TRUE', evidence_state: 'supports', independent_source_count: 1 })).summary).toContain('1 independent source.')
  })
})

describe('G8: "no evidence found" never looks like "evidence says false"', () => {
  const noEvidence = verdictVisual(base({ verdict: 'UNVERIFIABLE', evidence_state: 'no_evidence', abstain_reason: 'NO_EVIDENCE' }))
  const refuted = verdictVisual(base({ verdict: 'FALSE', evidence_state: 'refutes', independent_source_count: 2 }))

  it('no_evidence is neutral grey, dashed, search icon, not filled', () => {
    expect(noEvidence.key).toBe('no_evidence')
    expect(noEvidence.tone).toBe('neutral')
    expect(noEvidence.border).toBe('dashed')
    expect(noEvidence.icon).toBe('search')
    expect(noEvidence.filled).toBe(false)
  })

  it('no_evidence says explicitly it is NOT the same as false', () => {
    expect(noEvidence.badge).toBe('No evidence found — this is NOT the same as false')
    expect(noEvidence.badge).toBe(NO_EVIDENCE_BADGE)
    expect(`${noEvidence.headline} ${noEvidence.headlineSub}`).toBe('No evidence found — this is NOT the same as false')
    expect(noEvidence.summary).toContain('NOT the same as false')
  })

  it('shares no visual attribute with refuted FALSE', () => {
    expect(noEvidence.key).not.toBe(refuted.key)
    expect(noEvidence.tone).not.toBe(refuted.tone)
    expect(noEvidence.icon).not.toBe(refuted.icon)
    expect(noEvidence.border).not.toBe(refuted.border)
    expect(noEvidence.filled).not.toBe(refuted.filled)
  })

  it('is triggered by evidence_state alone or by abstain_reason alone', () => {
    expect(verdictVisual(base({ verdict: 'UNVERIFIABLE', evidence_state: 'no_evidence' })).key).toBe('no_evidence')
    expect(verdictVisual(base({ verdict: 'UNVERIFIABLE', evidence_state: 'insufficient', abstain_reason: 'NO_EVIDENCE' })).key).toBe('no_evidence')
  })

  it('a FALSE label with no evidence is still shown as no-evidence, with a G1 warning', () => {
    const v = verdictVisual(base({ verdict: 'FALSE', evidence_state: 'no_evidence' }))
    expect(v.key).toBe('no_evidence')
    expect(v.tone).not.toBe('false')
    expect(v.warning).toMatch(/G1/)
  })

  it('other abstains are amber, not grey and not red', () => {
    const v = verdictVisual(base({ verdict: 'UNVERIFIABLE', evidence_state: 'conflicting', abstain_reason: 'CONFLICT' }))
    expect(v.key).toBe('abstain')
    expect(v.tone).toBe('abstain')
    expect(v.icon).toBe('scale')
    expect(v.summary).toContain('not a judgement that the claim is true or false')
  })

  it('evidence-state copy distinguishes no_evidence from refutes', () => {
    for (const s of EVIDENCE_STATES) expect(EVIDENCE_STATE_COPY[s]).toBeTruthy()
    expect(EVIDENCE_STATE_COPY.no_evidence).not.toEqual(EVIDENCE_STATE_COPY.refutes)
    expect(EVIDENCE_STATE_COPY.no_evidence).toContain('NOT the same as false')
  })
})

describe('pending / failed and helpers', () => {
  it('no verdict yet is a pending state', () => {
    expect(verdictVisual(base({ status: 'processing' })).key).toBe('pending')
    expect(verdictVisual(base({ status: 'failed' })).headline).toBe('No verdict')
  })

  it('hides confidence for no-evidence and not-checkable', () => {
    expect(showConfidence('UNVERIFIABLE', 0.5, 'no_evidence')).toBe(false)
    expect(showConfidence('NOT_CHECKABLE', 0.9, 'not_checkable')).toBe(false)
    expect(showConfidence('FALSE', 0.88, 'false')).toBe(true)
    expect(showConfidence('TRUE', null, 'true')).toBe(false)
  })

  it('finds the abstain threshold by key name', () => {
    expect(findThreshold({ abstain_threshold: 0.7, manipulation_min: 0.8 })).toBe(0.7)
    expect(findThreshold({ ABSTAIN_THRESHOLD: 0.65 })).toBe(0.65)
    expect(findThreshold({})).toBeNull()
    expect(findThreshold(null)).toBeNull()
  })
})
