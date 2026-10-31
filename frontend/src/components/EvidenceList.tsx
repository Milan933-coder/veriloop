import type { EvidenceItem, EvidenceState, Stance } from '../api/types'
import { formatDate, pct, retrievalLabel } from '../lib/format'
import { EVIDENCE_STATE_COPY, NO_EVIDENCE_BADGE } from '../lib/verdict'
import { Icon, type IconName } from './Icon'
import { ExternalLink, Section } from './ui'

const STANCE: Record<Stance, { text: string; icon: IconName; cls: string }> = {
  support: { text: 'Supports', icon: 'check-circle', cls: 'border-true-line bg-true-soft text-true-fg' },
  refute: { text: 'Refutes', icon: 'x-octagon', cls: 'border-false-line bg-false-soft text-false-fg' },
  neutral: { text: 'Neutral', icon: 'info', cls: 'border-neutral-line bg-neutral-soft text-neutral-fg' },
  unknown: { text: 'Unclear', icon: 'help-circle', cls: 'border-dashed border-neutral-line bg-neutral-soft text-neutral-fg' },
}

export function StanceBadge({ stance, confidence }: { stance: Stance; confidence?: number }) {
  const s = STANCE[stance] ?? STANCE.unknown
  return (
    <span className={`chip ${s.cls}`} title={confidence ? `Stance confidence ${pct(confidence)}` : undefined}>
      <Icon name={s.icon} size={14} />
      {s.text}
    </span>
  )
}

/** Snippet with the stance quote highlighted (only when it is an exact substring, per contract). */
export function HighlightedSnippet({ snippet, quote }: { snippet: string; quote: string | null }) {
  const at = quote ? snippet.indexOf(quote) : -1
  if (!quote || at < 0) return <>{snippet}</>
  return (
    <>
      {snippet.slice(0, at)}
      <mark>{quote}</mark>
      {snippet.slice(at + quote.length)}
    </>
  )
}

function govLabel(item: EvidenceItem): string {
  return item.retrieval_method === 'pib_rss' ? 'Government source (PIB)' : 'Government source'
}

function EvidenceRow({ item, cited, groupSize }: { item: EvidenceItem; cited: boolean; groupSize: number }) {
  const cr = item.claimreview
  return (
    <li id={`ev-${item.id}`} className="ev-row rounded-lg border border-border bg-surface p-3 sm:p-4" tabIndex={-1}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded bg-fg px-1.5 py-0.5 font-mono text-xs font-bold text-surface">{item.id}</span>
        <span className="min-w-0 font-semibold">{item.source_name}</span>
        <span className="text-sm text-muted">{item.domain}</span>
        <StanceBadge stance={item.stance} confidence={item.stance_confidence} />
        {cited ? (
          <span className="chip border-primary/50 bg-primary-soft">
            <Icon name="link" size={14} /> Cited
          </span>
        ) : null}
        {item.is_government_source ? (
          <span className="chip border-border-strong">
            <Icon name="building" size={14} /> {govLabel(item)}
          </span>
        ) : null}
        {item.is_fact_check ? (
          <span className="chip border-border-strong">
            <Icon name="shield" size={14} /> Fact-check
          </span>
        ) : null}
      </div>

      {item.title ? <p className="mt-2 font-medium">{item.title}</p> : null}
      <p className="mt-1 text-sm leading-relaxed">
        <HighlightedSnippet snippet={item.snippet} quote={item.stance_quote} />
      </p>

      {cr ? (
        <p className="mt-2 rounded-md bg-surface-2 px-2 py-1 text-sm">
          <strong>{cr.publisher}</strong> rated it <strong>“{cr.rating_text}”</strong>
          {cr.manipulation_rating ? ' (manipulated media)' : ''} · similarity to this claim {pct(cr.similarity)}
          {cr.claimant ? ` · claimant: ${cr.claimant}` : ''}
        </p>
      ) : null}

      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
        <div>
          <dt className="text-muted">Published</dt>
          <dd className="font-medium">{formatDate(item.published_at)}</dd>
        </div>
        <div>
          <dt className="text-muted">Relevance</dt>
          <dd className="flex items-center gap-1.5 font-medium">
            <span className="relative h-1.5 w-12 overflow-hidden rounded bg-surface-2 ring-1 ring-border" aria-hidden="true">
              <span className="absolute inset-y-0 left-0 rounded bg-primary" style={{ width: `${Math.round(item.confidence_relevance * 100)}%` }} />
            </span>
            {pct(item.confidence_relevance)}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Retrieved via</dt>
          <dd className="font-medium">{retrievalLabel(item.retrieval_method)}</dd>
        </div>
        <div>
          <dt className="text-muted">Independence group</dt>
          <dd className="break-all font-medium">
            {item.publisher_group}
            {groupSize > 1 ? <span className="text-muted"> ({groupSize} items, counted once)</span> : null}
          </dd>
        </div>
      </dl>

      <p className="mt-2 text-sm">
        <ExternalLink href={item.url}>{item.url}</ExternalLink>
      </p>
    </li>
  )
}

export function EvidenceList({
  items,
  citedIds,
  evidenceState,
  independentCount,
}: {
  items: EvidenceItem[]
  citedIds: string[]
  evidenceState: EvidenceState | null
  independentCount: number
}) {
  const cited = new Set(citedIds)
  const groups = new Map<string, number>()
  for (const it of items) groups.set(it.publisher_group, (groups.get(it.publisher_group) ?? 0) + 1)

  const noEvidence = evidenceState === 'no_evidence' || items.length === 0
  return (
    <Section
      id="evidence"
      title="Evidence"
      icon="file"
      subtitle={
        noEvidence
          ? 'Sources searched: fact-check registry, news and wire feeds, government releases, GDELT, Wikidata.'
          : `${items.length} item${items.length === 1 ? '' : 's'} · ${groups.size} publisher group${groups.size === 1 ? '' : 's'} · ${independentCount} independent cited source${independentCount === 1 ? '' : 's'}. Links are exactly as returned by the tools.`
      }
      actions={
        evidenceState && !noEvidence ? <span className="chip border-border-strong">{EVIDENCE_STATE_COPY[evidenceState]}</span> : null
      }
    >
      {noEvidence ? (
        <div className="flex items-start gap-3 rounded-lg border-2 border-dashed border-neutral-line bg-neutral-soft p-4 text-neutral-fg" data-evidence-state="no_evidence">
          <Icon name="search" size={28} />
          <div>
            <p className="font-bold">{NO_EVIDENCE_BADGE}</p>
            <p className="mt-1 text-sm">
              No source returned anything relevant. Absence of reporting is not proof that a claim is false, so VeriLoop
              does not treat it as evidence either way.
            </p>
          </div>
        </div>
      ) : (
        <ol className="space-y-3" aria-label="Evidence items">
          {items.map((it) => (
            <EvidenceRow key={it.id} item={it} cited={cited.has(it.id)} groupSize={groups.get(it.publisher_group) ?? 1} />
          ))}
        </ol>
      )}
    </Section>
  )
}
