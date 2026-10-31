import type { AbstainReason, EvidenceState, JobStatus, VerdictLabel } from '../api/types'
import { abstainCopy } from './abstain'

/**
 * Verdict -> visual mapping. Colour is never the only signal: every state also has its own icon,
 * border style, fill style and words.
 *
 * G8: "no evidence found" must never look like "evidence says false". The no-evidence state is a
 * neutral grey, dashed, search-icon card that literally says it is NOT the same as false, and it wins
 * over the label whenever `evidence_state === "no_evidence"` or `abstain_reason === "NO_EVIDENCE"`.
 */
export type VisualKey = 'true' | 'false' | 'manipulated' | 'abstain' | 'no_evidence' | 'not_checkable' | 'pending'
export type VisualTone = 'true' | 'false' | 'manipulated' | 'abstain' | 'neutral' | 'notcheck' | 'pending'
export type VerdictIcon = 'check-circle' | 'x-octagon' | 'scan-face' | 'scale' | 'help-circle' | 'search' | 'ban' | 'loader'

export interface VerdictVisual {
  key: VisualKey
  tone: VisualTone
  icon: VerdictIcon
  /** card border style */
  border: 'solid' | 'dashed' | 'dotted'
  /** solid coloured header band (only for decisive verdicts) */
  filled: boolean
  /** big word(s) on the card */
  headline: string
  /** second line of the heading (G8: "— this is NOT the same as false") */
  headlineSub: string | null
  /** one-line explanation under the headline */
  summary: string
  /** compact text used in badges / screen readers; always self-explanatory without colour */
  badge: string
  /** shown when the API response is internally inconsistent */
  warning: string | null
}

export interface VerdictInput {
  status?: JobStatus | null
  verdict: VerdictLabel | null
  evidence_state: EvidenceState | null
  abstain_reason: AbstainReason | null
  independent_source_count: number
}

export const NO_EVIDENCE_BADGE = 'No evidence found — this is NOT the same as false'

function sources(n: number): string {
  return `${n} independent source${n === 1 ? '' : 's'}`
}

export function verdictVisual(v: VerdictInput): VerdictVisual {
  const n = Math.max(0, v.independent_source_count || 0)
  const noEvidence = v.evidence_state === 'no_evidence' || v.abstain_reason === 'NO_EVIDENCE'

  if (!v.verdict) {
    const failed = v.status === 'failed'
    return {
      key: 'pending',
      tone: 'pending',
      icon: failed ? 'help-circle' : 'loader',
      border: 'dotted',
      filled: false,
      headline: failed ? 'No verdict' : 'Checking…',
      headlineSub: null,
      summary: failed ? 'The run failed before a verdict was produced.' : 'Evidence, forensics and provenance are running in parallel.',
      badge: failed ? 'No verdict (run failed)' : 'Verification in progress',
      warning: null,
    }
  }

  if (noEvidence && v.verdict !== 'NOT_CHECKABLE') {
    const inconsistent = v.verdict !== 'UNVERIFIABLE'
    return {
      key: 'no_evidence',
      tone: 'neutral',
      icon: 'search',
      border: 'dashed',
      filled: false,
      headline: 'No evidence found',
      headlineSub: '— this is NOT the same as false',
      summary: 'This is NOT the same as false. None of the sources we searched said anything relevant about this claim.',
      badge: NO_EVIDENCE_BADGE,
      warning: inconsistent
        ? `The API returned ${v.verdict} with no evidence. A verdict without citations violates guardrail G1, so it is shown as "no evidence".`
        : null,
    }
  }

  switch (v.verdict) {
    case 'TRUE':
      return {
        key: 'true',
        tone: 'true',
        icon: 'check-circle',
        border: 'solid',
        filled: true,
        headline: 'True',
        headlineSub: null,
        summary: `Supported by ${sources(n)}.`,
        badge: `True — supported by ${sources(n)}`,
        warning: null,
      }
    case 'FALSE':
      return {
        key: 'false',
        tone: 'false',
        icon: 'x-octagon',
        border: 'solid',
        filled: true,
        headline: 'False',
        headlineSub: null,
        summary:
          v.evidence_state === 'refutes' || !v.evidence_state
            ? `Refuted by ${sources(n)}.`
            : `Judged false on ${sources(n)}.`,
        badge: `False — refuted by ${sources(n)}`,
        warning: null,
      }
    case 'MANIPULATED':
      return {
        key: 'manipulated',
        tone: 'manipulated',
        icon: 'scan-face',
        border: 'solid',
        filled: true,
        headline: 'Manipulated media',
        headlineSub: null,
        summary: `Synthetic or altered media, documented by ${sources(n)} and media forensics.`,
        badge: `Manipulated media — ${sources(n)}`,
        warning: null,
      }
    case 'NOT_CHECKABLE':
      return {
        key: 'not_checkable',
        tone: 'notcheck',
        icon: 'ban',
        border: 'dotted',
        filled: false,
        headline: 'Not checkable',
        headlineSub: null,
        summary: 'No verifiable factual claim was found in this content (for example opinion, satire or a recipe video).',
        badge: 'Not checkable — no factual claim found',
        warning: null,
      }
    case 'UNVERIFIABLE':
    default: {
      const conflict = v.evidence_state === 'conflicting' || v.abstain_reason === 'CONFLICT'
      const copy = abstainCopy(v.abstain_reason)
      return {
        key: 'abstain',
        tone: 'abstain',
        icon: conflict ? 'scale' : 'help-circle',
        border: 'solid',
        filled: false,
        headline: conflict ? 'Unverifiable: sources conflict' : 'Unverifiable',
        headlineSub: null,
        summary: `VeriLoop abstained${copy ? `: ${copy.title.toLowerCase()}` : ''}. This is not a judgement that the claim is true or false.`,
        badge: `Unverifiable (abstained)${copy ? ` — ${copy.title}` : ''}`,
        warning: null,
      }
    }
  }
}

export const EVIDENCE_STATE_COPY: Record<EvidenceState, string> = {
  supports: 'Evidence supports the claim',
  refutes: 'Evidence refutes the claim',
  manipulated: 'Evidence documents manipulation',
  conflicting: 'Evidence conflicts',
  insufficient: 'Some evidence, not enough to decide',
  no_evidence: NO_EVIDENCE_BADGE,
  not_checkable: 'No checkable claim',
}

/** Calibrated confidence is only meaningful for decisive labels and abstains with a score. */
export function showConfidence(verdict: VerdictLabel | null, confidence: number | null, key: VisualKey): boolean {
  if (confidence == null || Number.isNaN(confidence)) return false
  if (verdict === 'NOT_CHECKABLE' || key === 'no_evidence') return false
  return true
}

/** Find the abstain threshold in fusion.thresholds (key names are backend-defined). */
export function findThreshold(thresholds: Record<string, number> | null | undefined, name = 'abstain'): number | null {
  if (!thresholds) return null
  for (const [k, val] of Object.entries(thresholds)) {
    if (k.toLowerCase().includes(name) && typeof val === 'number' && val >= 0 && val <= 1) return val
  }
  return null
}
