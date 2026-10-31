import type { AbstainReason } from '../api/types'

export interface AbstainCopy {
  /** short heading */
  title: string
  /** plain-language explanation for a citizen */
  explanation: string
  /** what the user can do next */
  next: string
}

/**
 * Plain-language copy for every AbstainReason (BUILD_SPEC §7). `Record<AbstainReason, ...>` makes a
 * new enum value a compile error until it has copy; the unit test checks the same at runtime.
 */
export const ABSTAIN_COPY: Record<AbstainReason, AbstainCopy> = {
  NO_EVIDENCE: {
    title: 'No evidence found',
    explanation:
      'None of the sources we searched (fact-check registries, news wires, GDELT, government releases, reverse image search) said anything relevant about this claim. That does not mean the claim is false. It means nobody has reported on it yet, or it cannot be found.',
    next: 'Treat it with caution and do not forward it as fact. Check again later: new reporting may appear.',
  },
  LOW_CONFIDENCE: {
    title: 'Not enough confidence to decide',
    explanation:
      'We found relevant evidence, but after calibration the confidence was below the threshold VeriLoop needs before it will call a claim true or false.',
    next: 'Read the cited sources below and decide for yourself, or check again when more reporting is available.',
  },
  SINGLE_SOURCE: {
    title: 'Only one independent source',
    explanation:
      'VeriLoop needs at least two independent sources before issuing a verdict. Only one independent publisher (syndicated copies count once) addressed this claim.',
    next: 'One source is not enough to confirm or reject a claim. Wait for independent confirmation.',
  },
  CONFLICT: {
    title: 'Sources conflict',
    explanation:
      'Credible evidence points both ways: some sources support the claim and others contradict it, or news reports and media forensics disagree. Both sides are cited below.',
    next: 'Compare the cited sources on both sides. Do not share the claim as settled.',
  },
  FORENSIC_INCONCLUSIVE: {
    title: 'Media forensics inconclusive',
    explanation:
      'The deepfake detectors could not agree, or their scores fell inside the uncertainty band, and there was no strong text evidence either way. A weak forensic score is never turned into a "fake" verdict.',
    next: 'Look for the original upload or an official statement. Heavily compressed re-uploads are often impossible to judge.',
  },
  UNGROUNDED_RATIONALE: {
    title: 'Explanation failed the grounding check',
    explanation:
      'The verifier checks every sentence of the AI explanation against the stored evidence. At least one sentence was not supported by a cited source, so the verdict was rejected instead of shown.',
    next: 'The failing sentences are highlighted below. Use the cited evidence directly rather than the rejected explanation.',
  },
  JUDGE_DISAGREEMENT: {
    title: 'AI judge disagreed with the evidence',
    explanation:
      'The judge model reached a different conclusion from the weighted evidence score (or the independent second judge disagreed strongly), so VeriLoop declined to rule.',
    next: 'Review the evidence and both judges’ reasoning below.',
  },
  TOOL_OUTAGE: {
    title: 'Verification tools were unavailable',
    explanation:
      'Key verification services failed or timed out during this run. Rather than guess with partial information, VeriLoop abstained. Every failure is logged in the audit record.',
    next: 'Try again in a few minutes. The failed tools are listed below.',
  },
  LANGUAGE_UNSUPPORTED: {
    title: 'Language not sufficiently covered',
    explanation:
      'The claim is in a language for which we could not find enough multilingual evidence to make a reliable decision.',
    next: 'If you can, submit an English or Hindi version of the claim, or the original source link.',
  },
}

const FALLBACK: AbstainCopy = {
  title: 'VeriLoop abstained',
  explanation: 'VeriLoop could not reach a reliable verdict for this claim and chose not to guess.',
  next: 'Review the evidence below, if any.',
}

/** Copy for a reason string from the API. Unknown values (contract drift) get a generic message. */
export function abstainCopy(reason: string | null | undefined): AbstainCopy | null {
  if (!reason) return null
  return (ABSTAIN_COPY as Record<string, AbstainCopy | undefined>)[reason] ?? { ...FALLBACK, title: `${FALLBACK.title} (${reason})` }
}
