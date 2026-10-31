import type { EvidenceItem, JobView, RationaleSentence } from '../api/types'
import { abstainCopy } from '../lib/abstain'
import { degradedToolLabel, pct, toolLabel } from '../lib/format'
import { findThreshold, showConfidence, verdictVisual, type VerdictVisual, type VisualTone } from '../lib/verdict'
import { Icon } from './Icon'
import { Callout, Meter } from './ui'

/** Complete class strings per tone (kept literal so Tailwind can see them). */
export const TONE_CLASSES: Record<VisualTone, { header: string; line: string; text: string; meter: 'true' | 'false' | 'manip' | 'abstain' | 'neutral' | 'primary' }> = {
  true: { header: 'bg-true-solid text-white', line: 'border-true-line', text: 'text-true-fg', meter: 'true' },
  false: { header: 'bg-false-solid text-white', line: 'border-false-line', text: 'text-false-fg', meter: 'false' },
  manipulated: { header: 'bg-manip-solid text-white', line: 'border-manip-line', text: 'text-manip-fg', meter: 'manip' },
  abstain: { header: 'bg-abstain-soft text-abstain-fg', line: 'border-abstain-line', text: 'text-abstain-fg', meter: 'abstain' },
  neutral: { header: 'bg-neutral-soft text-neutral-fg', line: 'border-neutral-line', text: 'text-neutral-fg', meter: 'neutral' },
  notcheck: { header: 'bg-notcheck-soft text-notcheck-fg', line: 'border-notcheck-line', text: 'text-notcheck-fg', meter: 'neutral' },
  pending: { header: 'bg-surface-2 text-fg', line: 'border-border-strong', text: 'text-fg', meter: 'primary' },
}

const BORDER_STYLE: Record<VerdictVisual['border'], string> = {
  solid: 'border-solid',
  dashed: 'border-dashed',
  dotted: 'border-dotted',
}

export function CitationChip({ id, item }: { id: string; item: EvidenceItem | undefined }) {
  if (!item) {
    return (
      <span className="chip border-dashed text-muted line-through" title="This id is not among the stored evidence for this job">
        {id}
      </span>
    )
  }
  return (
    <a
      href={`#ev-${id}`}
      className="chip border-primary/50 bg-primary-soft text-fg no-underline hover:border-primary"
      aria-label={`Evidence ${id}: ${item.source_name}`}
      title={`${item.source_name}${item.title ? `: ${item.title}` : ''}`}
    >
      {id}
    </a>
  )
}

function RationaleList({
  sentences,
  sourceIndex,
  failing,
  failReasons,
}: {
  sentences: RationaleSentence[]
  sourceIndex: Map<string, EvidenceItem>
  failing: Set<number>
  failReasons: Map<number, string>
}) {
  return (
    <ol className="space-y-2">
      {sentences.map((s, i) => {
        const bad = failing.has(i)
        return (
          <li key={i} className={bad ? 'rounded-md border-l-4 border-false-line bg-false-soft p-2 text-false-fg' : 'pl-1'}>
            {bad ? (
              <span className="mr-1 inline-flex items-center gap-1 text-xs font-bold uppercase">
                <Icon name="x-octagon" size={14} /> Not grounded:
              </span>
            ) : null}
            <span>{s.text}</span>{' '}
            <span className="inline-flex flex-wrap gap-1 align-middle">
              {s.evidence_ids.map((id) => (
                <CitationChip key={id} id={id} item={sourceIndex.get(id)} />
              ))}
              {s.evidence_ids.length === 0 ? <span className="chip border-dashed text-muted">no citation</span> : null}
            </span>
            {bad && failReasons.get(i) ? <span className="mt-1 block text-xs">Verifier: {failReasons.get(i)}</span> : null}
          </li>
        )
      })}
    </ol>
  )
}

function failingIndexes(job: JobView, sentences: RationaleSentence[]): { failing: Set<number>; reasons: Map<number, string> } {
  const failing = new Set<number>()
  const reasons = new Map<number, string>()
  const v = job.debate?.verifier
  if (!v) return { failing, reasons }
  const failingTexts = new Set(v.failing_sentences.map((t) => t.trim()))
  sentences.forEach((s, i) => {
    if (failingTexts.has(s.text.trim())) failing.add(i)
  })
  for (const c of v.checks) {
    const idx = sentences.findIndex((s) => s.text.trim() === c.text.trim())
    const i = idx >= 0 ? idx : c.sentence_index
    if (!c.ok && i >= 0 && i < sentences.length) {
      failing.add(i)
      const why = [c.reason, c.nli_entailment != null ? `NLI entailment ${c.nli_entailment.toFixed(2)}` : '', !c.deterministic_ok ? 'citation check failed' : '']
        .filter(Boolean)
        .join('; ')
      if (why) reasons.set(i, why)
    }
  }
  return { failing, reasons }
}

export function VerdictCard({ job, sourceIndex }: { job: JobView; sourceIndex: Map<string, EvidenceItem> }) {
  const vis = verdictVisual(job)
  const tone = TONE_CLASSES[vis.tone]
  const copy = abstainCopy(job.abstain_reason)
  const threshold = findThreshold(job.fusion?.thresholds)
  const primaryClaim = job.claim?.claims.find((c) => c.id === job.claim?.primary_claim_id) ?? job.claim?.claims[0]
  const ungrounded = job.abstain_reason === 'UNGROUNDED_RATIONALE'
  const judgeRationale = job.debate?.judge?.rationale ?? []
  const showRejected = ungrounded && job.rationale.length === 0 && judgeRationale.length > 0
  const sentences = showRejected ? judgeRationale : job.rationale
  const { failing, reasons } = ungrounded ? failingIndexes(job, sentences) : { failing: new Set<number>(), reasons: new Map<number, string>() }
  const degraded = job.degraded_tools.map(degradedToolLabel)
  const labels = job.labels.filter((l) => !degraded.some((d) => d.toLowerCase() === l.toLowerCase()))
  const judge = job.debate?.judge
  const second = job.debate?.second_judge
  const confVisible = showConfidence(job.verdict, job.confidence, vis.key)
  const decisive = vis.key === 'true' || vis.key === 'false' || vis.key === 'manipulated'

  return (
    <section
      aria-labelledby="verdict-title"
      data-verdict-style={vis.key}
      className={`overflow-hidden rounded-xl border-2 bg-surface ${BORDER_STYLE[vis.border]} ${tone.line}`}
    >
      <div className={`flex items-start gap-3 p-4 sm:gap-4 sm:p-5 ${tone.header}`}>
        <Icon name={vis.icon} size={44} className="mt-1" />
        <div className="min-w-0">
          <p className="text-xs font-bold uppercase tracking-wider opacity-90">Verdict</p>
          <h2 id="verdict-title" className="text-2xl font-extrabold leading-tight sm:text-3xl">
            {vis.headline}
            {vis.headlineSub ? <span className="mt-0.5 block text-lg font-bold sm:text-xl">{vis.headlineSub}</span> : null}
          </h2>
          <p className="mt-1 font-medium">{vis.summary}</p>
        </div>
      </div>

      <div className="space-y-5 p-4 sm:p-5">
        {primaryClaim ? (
          <figure>
            <figcaption className="eyebrow">Claim checked</figcaption>
            <blockquote className="mt-1 border-l-4 border-border-strong pl-3 text-base font-medium">
              “{primaryClaim.text}”
              {primaryClaim.text_en && primaryClaim.text_en !== primaryClaim.text ? (
                <span className="mt-1 block text-sm font-normal text-muted">English: “{primaryClaim.text_en}”</span>
              ) : null}
            </blockquote>
          </figure>
        ) : null}

        {vis.warning ? <Callout tone="warn" title="Inconsistent response">{vis.warning}</Callout> : null}

        {confVisible && job.confidence != null ? (
          <div>
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <span className="text-sm font-semibold">{decisive ? 'Calibrated confidence in this verdict' : 'Best-supported label confidence'}</span>
              <span className="text-xl font-extrabold tabular-nums">{pct(job.confidence)}</span>
            </div>
            <Meter
              value={job.confidence}
              label="Calibrated confidence"
              tone={tone.meter}
              marker={threshold}
              markerLabel={threshold != null ? `Verdict threshold ${pct(threshold)}` : undefined}
            />
            <p className="mt-1 text-xs text-muted">
              {threshold != null ? `The marker is the ${pct(threshold)} threshold needed to issue a verdict. ` : ''}
              {decisive
                ? 'Probability calibrated on a labelled test set, not a raw model score.'
                : threshold != null && job.confidence < threshold
                  ? 'Below the threshold, so VeriLoop abstained instead of guessing.'
                  : 'VeriLoop abstained for the reason below even though the score alone was high enough.'}
            </p>
          </div>
        ) : null}

        {copy && vis.key !== 'not_checkable' ? (
          <div className={`rounded-lg border p-3 ${vis.key === 'no_evidence' ? 'border-dashed border-neutral-line bg-neutral-soft' : 'border-abstain-line bg-abstain-soft'}`}>
            <p className={`flex items-center gap-2 font-bold ${vis.key === 'no_evidence' ? 'text-neutral-fg' : 'text-abstain-fg'}`}>
              <Icon name={vis.key === 'no_evidence' ? 'search' : 'help-circle'} size={18} />
              Why VeriLoop abstained: {copy.title}
            </p>
            <p className="mt-1 text-sm">{copy.explanation}</p>
            {job.abstain_detail ? <p className="mt-1 text-sm text-muted">Details: {job.abstain_detail}</p> : null}
            <p className="mt-1 text-sm font-medium">What you can do: {copy.next}</p>
          </div>
        ) : null}

        {vis.key === 'not_checkable' ? (
          <div className="rounded-lg border border-dotted border-notcheck-line bg-notcheck-soft p-3 text-sm text-notcheck-fg">
            <p className="font-bold">No factual claim to verify</p>
            {job.abstain_detail ? <p className="mt-1">{job.abstain_detail}</p> : null}
            {job.claim?.worthiness_reason ? (
              <p className="mt-1">
                Check-worthiness {pct(job.claim.check_worthiness)}: {job.claim.worthiness_reason}
              </p>
            ) : null}
          </div>
        ) : null}

        {sentences.length ? (
          <div>
            <h3 className="eyebrow mb-2">{showRejected ? 'Rejected explanation (failed the grounding check, not used)' : 'Why: every sentence is tied to cited evidence'}</h3>
            <RationaleList sentences={sentences} sourceIndex={sourceIndex} failing={failing} failReasons={reasons} />
            {judge?.extractive ? <p className="mt-2 text-xs text-muted">Extractive rationale: quoted evidence sentences (LLM disabled).</p> : null}
          </div>
        ) : null}

        {job.abstain_reason === 'JUDGE_DISAGREEMENT' && judge ? (
          <Callout tone="warn" title="Judges disagreed">
            The AI judge said <strong>{judge.label}</strong>
            {job.fusion ? (
              <>
                {' '}while the weighted evidence pointed to <strong>{job.fusion.candidate}</strong>
              </>
            ) : null}
            . VeriLoop does not pick a side when they disagree.
          </Callout>
        ) : null}

        {judge || second ? (
          <div className="flex flex-wrap gap-2 text-sm">
            {judge ? (
              <span className="chip">
                <Icon name="gavel" size={14} /> Judge <code>{judge.model_id}</code>: {judge.label}
              </span>
            ) : null}
            {second ? (
              <span className={`chip ${second.agrees === false ? 'border-abstain-line bg-abstain-soft text-abstain-fg' : ''}`}>
                <Icon name={second.error ? 'alert' : second.agrees ? 'check-circle' : 'x-octagon'} size={14} />
                Second judge <code>{second.model_id}</code>:{' '}
                {second.error ? `unavailable (${second.error})` : `${second.label ?? 'n/a'}${second.agrees == null ? '' : second.agrees ? ' (agrees)' : ' (disagrees: confidence reduced)'}`}
              </span>
            ) : null}
          </div>
        ) : null}

        {degraded.length ? (
          <Callout tone="warn" title="Degraded result">
            <ul className="list-disc pl-4">
              {degraded.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          </Callout>
        ) : null}

        {labels.length ? (
          <div>
            <h3 className="eyebrow mb-1.5">Caveats</h3>
            <ul className="flex flex-wrap gap-2">
              {labels.map((l) => (
                <li key={l} className="chip whitespace-normal border-border-strong">
                  <Icon name="info" size={14} />
                  {l}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {job.tool_errors.length ? (
          <details className="text-sm">
            <summary className="cursor-pointer font-semibold text-muted">
              {job.tool_errors.length} logged tool failure{job.tool_errors.length === 1 ? '' : 's'} (never replaced with invented data)
            </summary>
            <ul className="mt-2 space-y-1">
              {job.tool_errors.map((e, i) => (
                <li key={i} className="break-words">
                  <strong>{toolLabel(e.tool)}</strong>
                  {e.op ? ` / ${e.op}` : ''}: {e.error_type}: {e.message}
                  {e.attempts > 1 ? ` (${e.attempts} attempts)` : ''}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </div>
    </section>
  )
}
