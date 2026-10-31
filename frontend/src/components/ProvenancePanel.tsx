import { Link } from 'react-router'
import type { Appearance, ProvenanceResult } from '../api/types'
import { formatDateTime, humanize, num, pct } from '../lib/format'
import { Icon } from './Icon'
import { EmptyNote, ExternalLink, Meter, Section } from './ui'

const FLAG_TEXT: Record<string, string> = {
  satire: 'Satire site',
  state_affiliated: 'State-affiliated outlet',
  known_disinfo: 'Known disinformation source',
}

export function ContextFlag({ flag }: { flag: string }) {
  return (
    <span className="chip border-abstain-line bg-abstain-soft text-abstain-fg">
      <Icon name="flag" size={14} />
      {FLAG_TEXT[flag] ?? humanize(flag)}
    </span>
  )
}

const MATCH_TEXT: Record<string, string> = {
  full: 'full image match',
  partial: 'partial match',
  page: 'page with matching image',
  phash: 'pHash near-duplicate',
  clip: 'CLIP semantic match',
}

function AppearanceItem({ a, first }: { a: Appearance; first?: boolean }) {
  return (
    <div className="min-w-0">
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <strong className="tabular-nums">{a.first_seen_at ? formatDateTime(a.first_seen_at) : 'Date unknown'}</strong>
        <span className="text-muted">
          {MATCH_TEXT[a.match_type] ?? a.match_type} · date from {a.timestamp_source.replace(/_/g, ' ')}
        </span>
        {first ? <span className="chip border-primary/50 bg-primary-soft">Earliest</span> : null}
        {a.context_flags.map((f) => (
          <ContextFlag key={f} flag={f} />
        ))}
      </p>
      {a.page_title ? <p className="mt-0.5 text-sm font-medium">{a.page_title}</p> : null}
      <p className="text-sm">
        <ExternalLink href={a.url}>{a.url}</ExternalLink>
      </p>
    </div>
  )
}

export function ProvenancePanel({ provenance }: { provenance: ProvenanceResult | null }) {
  if (!provenance) {
    return (
      <Section id="provenance" title="Provenance" icon="globe">
        <EmptyNote>Provenance did not run for this input.</EmptyNote>
      </Section>
    )
  }
  const p = provenance
  const sorted = [...p.appearances].sort((a, b) => {
    const ta = a.first_seen_at ? Date.parse(a.first_seen_at) : Infinity
    const tb = b.first_seen_at ? Date.parse(b.first_seen_at) : Infinity
    return ta - tb
  })
  const empty = !p.first_seen_estimate && !sorted.length && !p.known_fake_matches.length && !p.prior_run_matches.length

  return (
    <Section
      id="provenance"
      title="Provenance"
      icon="globe"
      subtitle="Where this media appeared before: own pHash/CLIP index of known fakes, then reverse image search."
      actions={<span className="chip">{humanize(p.branch_status)}</span>}
    >
      <div className="space-y-5">
        {p.context_flags.length ? (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-semibold">Context:</span>
            {p.context_flags.map((f) => (
              <ContextFlag key={f} flag={f} />
            ))}
          </div>
        ) : null}

        {p.known_fake_matches.length ? (
          <div>
            <h3 className="eyebrow mb-2">Matches a known fake</h3>
            <ul className="space-y-2">
              {p.known_fake_matches.map((k) => (
                <li key={k.known_fake_id} className="rounded-lg border border-manip-line bg-manip-soft p-3 text-sm text-fg">
                  <p className="flex flex-wrap items-center gap-2">
                    <Icon name="scan-face" size={18} className="text-manip-fg" />
                    <strong className="font-mono">{k.known_fake_id}</strong>
                    <span className="chip border-manip-line">{humanize(k.label)}</span>
                  </p>
                  {k.title ? <p className="mt-1 font-medium">{k.title}</p> : null}
                  <p className="mt-1 tabular-nums">
                    {k.hamming != null ? (
                      <>
                        pHash distance <strong>{k.hamming}</strong> of 64 bits {k.hamming <= 8 ? '(near-identical)' : ''}
                      </>
                    ) : null}
                    {k.hamming != null && k.clip_similarity != null ? ' · ' : ''}
                    {k.clip_similarity != null ? (
                      <>
                        CLIP similarity <strong>{num(k.clip_similarity)}</strong>
                      </>
                    ) : null}
                  </p>
                  {k.notes ? <p className="mt-1 text-muted">{k.notes}</p> : null}
                  {k.source_urls.length ? (
                    <ul className="mt-1 space-y-0.5">
                      {k.source_urls.map((u) => (
                        <li key={u}>
                          <ExternalLink href={u}>{u}</ExternalLink>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {p.first_seen_estimate ? (
          <div className="rounded-lg border border-border bg-surface-2 p-3">
            <h3 className="eyebrow mb-1">First-seen estimate</h3>
            <AppearanceItem a={p.first_seen_estimate} />
          </div>
        ) : null}

        {p.out_of_context_score > 0 ? (
          <div>
            <div className="mb-1 flex justify-between text-sm">
              <span className="font-semibold">Out-of-context score</span>
              <span className="font-bold tabular-nums">{pct(p.out_of_context_score)}</span>
            </div>
            <Meter value={p.out_of_context_score} label="Out-of-context score" tone="abstain" />
            <p className="mt-1 text-xs text-muted">How strongly the earliest appearance predates the event the post claims to show.</p>
          </div>
        ) : null}

        {sorted.length ? (
          <div>
            <h3 className="eyebrow mb-2">Appearances timeline</h3>
            <ol className="relative space-y-3 border-l-2 border-border pl-4">
              {sorted.map((a, i) => (
                <li key={`${a.url}-${i}`} className="relative">
                  <span className="absolute -left-[1.4rem] top-1.5 h-3 w-3 rounded-full border-2 border-surface bg-primary" aria-hidden="true" />
                  <AppearanceItem a={a} first={i === 0 && !!a.first_seen_at} />
                </li>
              ))}
            </ol>
          </div>
        ) : null}

        {p.prior_run_matches.length ? (
          <p className="text-sm">
            <span className="font-semibold">Seen in earlier VeriLoop runs: </span>
            {p.prior_run_matches.map((id, i) => (
              <span key={id}>
                {i > 0 ? ', ' : ''}
                <Link to={`/j/${encodeURIComponent(id)}`}>{id}</Link>
              </span>
            ))}
          </p>
        ) : null}

        {empty ? <EmptyNote>No prior appearances or known-fake matches were found.</EmptyNote> : null}
        {p.notes.length ? (
          <ul className="list-disc space-y-1 pl-5 text-sm text-muted">
            {p.notes.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        ) : null}
      </div>
    </Section>
  )
}
