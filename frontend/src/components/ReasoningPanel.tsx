import type { Argument, DebateResult, EvidenceItem, FusionResult, SignalScore } from '../api/types'
import { formatMs, humanize, num, pct, signed } from '../lib/format'
import { CitationChip } from './VerdictCard'
import { Icon } from './Icon'
import { Disclosure, KeyValue, Section } from './ui'

const SIGNAL_TEXT: Record<string, string> = {
  claimreview: 'Fact-check (ClaimReview)',
  news_stance: 'News & wire stance',
  gdelt: 'GDELT corroboration',
  wikidata: 'Wikidata consistency',
  provenance: 'Provenance',
  forensic: 'Forensic P(fake)',
}

/** -1..+1 diverging bar around a neutral midpoint (refutes left, supports right). */
function SignalBar({ s }: { s: SignalScore }) {
  if (!s.present) {
    return (
      <div className="flex h-5 items-center rounded border border-dashed border-border-strong px-2 text-xs text-muted">
        absent: excluded, never counted as negative
      </div>
    )
  }
  if (s.name === 'forensic') {
    return (
      <div className="relative h-5" title={`P(fake) ${num(s.score)}`} aria-hidden="true">
        <div className="absolute inset-x-0 top-1/2 h-px bg-border" />
        <div className="absolute left-0 top-1/2 h-2 -translate-y-1/2 rounded-r bg-manip-solid" style={{ width: `${Math.max(0, Math.min(1, s.score)) * 100}%` }} />
      </div>
    )
  }
  const v = Math.max(-1, Math.min(1, s.score))
  const width = Math.abs(v) * 50
  return (
    <div className="relative h-5" title={`${signed(v)} (${v >= 0 ? 'supports' : 'refutes'})`} aria-hidden="true">
      <div className="absolute inset-x-0 top-1/2 h-px bg-border" />
      <div className="absolute inset-y-0 left-1/2 w-px bg-border-strong" />
      <div
        className={`absolute top-1/2 h-2 -translate-y-1/2 ${v >= 0 ? 'rounded-r bg-true-solid' : 'rounded-l bg-false-solid'}`}
        style={{ left: v >= 0 ? '50%' : `${50 - width}%`, width: `${width}%` }}
      />
    </div>
  )
}

function Signals({ fusion, sourceIndex }: { fusion: FusionResult; sourceIndex: Map<string, EvidenceItem> }) {
  return (
    <div>
      <h3 className="eyebrow mb-1">Weighted signals</h3>
      <p className="mb-2 text-xs text-muted">Left = refutes, right = supports. Weights are configurable and printed in the audit record.</p>
      <ul className="space-y-3">
        {fusion.signals.map((s) => (
          <li key={s.name} className="grid grid-cols-1 gap-x-3 gap-y-1 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)_4rem] sm:items-center">
            <div className="min-w-0 text-sm">
              <p className="font-semibold">{SIGNAL_TEXT[s.name] ?? humanize(s.name)}</p>
              <p className="text-xs text-muted">
                weight {num(s.weight)}
                {s.reliability !== 1 ? ` · reliability ${num(s.reliability)}` : ''}
              </p>
            </div>
            <SignalBar s={s} />
            <p className="text-sm font-semibold tabular-nums sm:text-right">{s.present ? (s.name === 'forensic' ? num(s.score) : signed(s.score)) : '—'}</p>
            {s.detail || s.evidence_ids.length ? (
              <p className="flex flex-wrap items-center gap-1 text-xs text-muted sm:col-span-3">
                {s.detail}
                {s.evidence_ids.map((id) => (
                  <CitationChip key={id} id={id} item={sourceIndex.get(id)} />
                ))}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ArgumentBlock({ title, arg, sourceIndex }: { title: string; arg: Argument; sourceIndex: Map<string, EvidenceItem> }) {
  return (
    <div className="rounded-lg border border-border p-3">
      <h4 className="mb-1 font-semibold">{title}</h4>
      <ul className="list-disc space-y-1 pl-5 text-sm">
        {arg.points.map((p, i) => (
          <li key={i}>
            {p.text}{' '}
            {p.evidence_ids.map((id) => (
              <CitationChip key={id} id={id} item={sourceIndex.get(id)} />
            ))}
          </li>
        ))}
      </ul>
      {arg.followup_queries.length ? <p className="mt-2 text-xs text-muted">Follow-up searches: {arg.followup_queries.join(' · ')}</p> : null}
    </div>
  )
}

function Debate({ debate, sourceIndex }: { debate: DebateResult; sourceIndex: Map<string, EvidenceItem> }) {
  if (debate.skipped) {
    return (
      <p className="text-sm text-muted">
        <Icon name="info" size={14} className="mr-1 inline" />
        Debate skipped{debate.skip_reason ? `: ${debate.skip_reason}` : ''}
      </p>
    )
  }
  const v = debate.verifier
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">
        {debate.proponent ? <ArgumentBlock title="Proponent (argues TRUE)" arg={debate.proponent} sourceIndex={sourceIndex} /> : null}
        {debate.opponent ? <ArgumentBlock title="Opponent (argues FALSE)" arg={debate.opponent} sourceIndex={sourceIndex} /> : null}
      </div>
      {debate.judge ? (
        <p className="text-sm">
          <strong>Judge</strong> <code>{debate.judge.model_id}</code> ({debate.judge.provider}) ruled <strong>{debate.judge.label}</strong>
          {debate.judge.extractive ? ' (extractive mode)' : ''}.
        </p>
      ) : null}
      {v ? (
        <div>
          <p className={`flex items-center gap-2 text-sm font-semibold ${v.ok ? 'text-true-fg' : 'text-false-fg'}`}>
            <Icon name={v.ok ? 'check-circle' : 'x-octagon'} size={16} />
            Verifier: {v.ok ? 'every sentence grounded' : `${v.failing_sentences.length || v.checks.filter((c) => !c.ok).length} sentence(s) not grounded`}
            {v.methods.length ? <span className="font-normal text-muted">({v.methods.join(' + ')})</span> : null}
          </p>
          {v.checks.length ? (
            <ul className="mt-2 space-y-1 text-sm">
              {v.checks.map((c) => (
                <li key={c.sentence_index} className="flex gap-2">
                  <Icon name={c.ok ? 'check-circle' : 'x-octagon'} size={16} className={`mt-0.5 ${c.ok ? 'text-true-fg' : 'text-false-fg'}`} />
                  <span className="min-w-0">
                    <span className="sr-only">{c.ok ? 'Grounded: ' : 'Not grounded: '}</span>
                    {c.text}{' '}
                    <span className="text-xs text-muted">
                      [ids {c.deterministic_ok ? 'ok' : 'fail'} · LLM {c.llm_supported == null ? 'n/a' : c.llm_supported ? 'supported' : 'unsupported'} · NLI{' '}
                      {c.nli_entailment == null ? 'n/a' : num(c.nli_entailment)}]{c.reason ? ` ${c.reason}` : ''}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {debate.followup_items.length ? <p className="text-xs text-muted">Debate added {debate.followup_items.length} follow-up evidence item(s), listed in Evidence.</p> : null}
      {debate.latency_ms ? <p className="text-xs text-muted">Debate time {formatMs(debate.latency_ms)}</p> : null}
    </div>
  )
}

export function ReasoningPanel({
  fusion,
  debate,
  sourceIndex,
}: {
  fusion: FusionResult | null
  debate: DebateResult | null
  sourceIndex: Map<string, EvidenceItem>
}) {
  if (!fusion && !debate) return null
  return (
    <Section id="reasoning" title="How the decision was made" icon="scale" subtitle="Weighted evidence fusion, calibration, multi-agent debate and the grounding verifier.">
      <div className="space-y-5">
        {fusion ? (
          <>
            <KeyValue
              items={[
                ['Evidence fusion suggests', `${fusion.candidate}${fusion.candidate_reason ? ` (${fusion.candidate_reason})` : ''}${fusion.fast_path ? ' · fast path (fact-check match)' : ''}`],
                ['Independent groups', `${fusion.groups_support} support · ${fusion.groups_refute} refute · ${fusion.groups_manipulation} document manipulation`],
                ['Scores', `truth T ${signed(fusion.truth_score)} · calibrated P(true) ${pct(fusion.p_true)} · manipulation M ${num(fusion.manipulation_score)}`],
                Object.keys(fusion.thresholds).length
                  ? ['Thresholds', Object.entries(fusion.thresholds).map(([k, val]) => `${k} ${num(val)}`).join(' · ')]
                  : null,
                ['Versions', `weights ${fusion.weights_version || 'n/a'} · calibration ${fusion.calibration_version || 'n/a'}`],
              ]}
            />
            {fusion.signals.length ? <Signals fusion={fusion} sourceIndex={sourceIndex} /> : null}
            {fusion.notes.length ? (
              <ul className="list-disc pl-5 text-sm text-muted">
                {fusion.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}
        {debate ? (
          <Disclosure summary="Multi-agent debate & verifier" defaultOpen={!!debate.verifier && !debate.verifier.ok}>
            <Debate debate={debate} sourceIndex={sourceIndex} />
          </Disclosure>
        ) : null}
      </div>
    </Section>
  )
}
