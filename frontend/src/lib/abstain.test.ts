import { describe, expect, it } from 'vitest'
import { ABSTAIN_REASONS } from '../api/types'
import { ABSTAIN_COPY, abstainCopy } from './abstain'

describe('abstain reason copy', () => {
  it.each(ABSTAIN_REASONS)('%s has a title, a plain-language explanation and a next step', (reason) => {
    const c = abstainCopy(reason)
    expect(c).not.toBeNull()
    expect(c!.title.length).toBeGreaterThan(5)
    expect(c!.explanation.length).toBeGreaterThan(40)
    expect(c!.next.length).toBeGreaterThan(10)
    // plain language: no raw enum names or underscores in the user-facing text
    expect(c!.title).not.toMatch(/_/)
    expect(c!.explanation).not.toContain(reason)
  })

  it('has copy for exactly the AbstainReason enum values (no stale keys)', () => {
    expect(Object.keys(ABSTAIN_COPY).sort()).toEqual([...ABSTAIN_REASONS].sort())
  })

  it('titles are unique so reasons are distinguishable', () => {
    const titles = ABSTAIN_REASONS.map((r) => ABSTAIN_COPY[r].title)
    expect(new Set(titles).size).toBe(titles.length)
  })

  it('NO_EVIDENCE copy says it is not the same as false (G8)', () => {
    expect(ABSTAIN_COPY.NO_EVIDENCE.explanation).toMatch(/does not mean the claim is false/i)
  })

  it('returns null for no reason and a generic message for unknown values', () => {
    expect(abstainCopy(null)).toBeNull()
    expect(abstainCopy(undefined)).toBeNull()
    expect(abstainCopy('SOMETHING_NEW')?.title).toContain('SOMETHING_NEW')
  })
})
