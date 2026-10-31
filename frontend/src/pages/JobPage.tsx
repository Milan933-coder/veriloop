import { useMemo } from 'react'
import { Link, useParams } from 'react-router'
import { links } from '../api/client'
import type { JobView } from '../api/types'
import { EvidenceList } from '../components/EvidenceList'
import { ForensicPanel } from '../components/ForensicPanel'
import { Icon } from '../components/Icon'
import { LegalWizard } from '../components/LegalWizard'
import { ProvenancePanel } from '../components/ProvenancePanel'
import { ReasoningPanel } from '../components/ReasoningPanel'
import { ReportFlow } from '../components/ReportFlow'
import { TraceTimeline } from '../components/TraceTimeline'
import { Callout, CopyButton, Disclosure, ExternalLink, KeyValue, Section, Spinner } from '../components/ui'
import { VerdictCard } from '../components/VerdictCard'
import { isReplay } from '../hooks/useHealth'
import { isTerminalStatus, useJob } from '../hooks/useJob'
import { usePageTitle } from '../hooks/usePageTitle'
import { formatDateTime, pct, rateLimitedTool, toolLabel } from '../lib/format'
import { MOCK } from '../lib/mockMode'
import { PLATFORM_LABELS } from '../lib/platform'
import { verdictVisual } from '../lib/verdict'
import { allEvidence } from '../lib/evidence'

function StatusPanel({ job }: { job: JobView }) {
  const limited = rateLimitedTool(job.status_detail)
  if (job.status === 'queued') {
    return (
      <Callout tone="info" icon="clock" role="status" title="Queued">
        {job.queue_position ? `Position ${job.queue_position} in the queue. ` : ''}Your check starts as soon as a worker is
        free. This page updates automatically.
      </Callout>
    )
  }
  if (job.status === 'processing') {
    return (
      <Callout tone={limited ? 'warn' : 'info'} icon={limited ? 'clock' : 'loader'} role="status" title={limited ? `Processing (rate-limited: ${toolLabel(limited)})` : 'Processing'}>
        {limited
          ? `${toolLabel(limited)} asked us to slow down. VeriLoop is waiting and retrying instead of skipping it. This page keeps updating; you can also come back later to the same link.`
          : job.status_detail ?? 'Evidence, forensics and provenance are being checked in parallel.'}
      </Callout>
    )
  }
  if (job.status === 'failed') {
    return (
      <Callout tone="danger" role="alert" title="The run failed">
        {job.error ?? 'An internal error stopped this run.'} {job.audit_id != null ? 'The failure itself was written to the audit log.' : ''}
      </Callout>
    )
  }
  return null
}

function InputHeader({ job }: { job: JobView }) {
  const input = job.input
  const claim = job.claim
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Link to="/" className="inline-flex items-center gap-1 font-semibold">
          <Icon name="arrow-right" size={16} className="rotate-180" /> New check
        </Link>
        <span className="text-muted">·</span>
        <span className="text-muted">Job</span>
        <code className="break-all">{job.job_id}</code>
        <CopyButton text={job.job_id} label="Copy id" className="min-h-8 px-2" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {input ? <span className="chip">{PLATFORM_LABELS[input.platform] ?? input.platform}</span> : null}
        <span className="chip">source: {job.source}</span>
        <span className="chip">{formatDateTime(job.created_at)}</span>
        {isReplay(job.mode) ? <span className="chip border-replay-bg bg-replay-bg text-replay-fg">REPLAY MODE</span> : null}
        {MOCK ? <span className="chip border-mock-fg bg-mock-bg text-mock-fg">MOCK DATA</span> : null}
        {job.redacted ? <span className="chip">content redacted (privacy TTL)</span> : null}
      </div>
      {input?.title || input?.url ? (
        <p className="text-sm">
          {input.title ? <span className="font-semibold">{input.title} </span> : null}
          {input.url ? <ExternalLink href={input.url}>{input.url}</ExternalLink> : null}
        </p>
      ) : null}
      {claim && (claim.caption_text || claim.transcript || claim.ocr_text || claim.claims.length > 1) ? (
        <Disclosure summary="What VeriLoop extracted from the content">
          <KeyValue
            items={[
              claim.caption_text ? ['Caption / text', claim.caption_text] : null,
              claim.transcript ? [`Speech${claim.transcript_language ? ` (${claim.transcript_language})` : ''}`, claim.transcript] : null,
              claim.ocr_text ? ['On-screen text', claim.ocr_text] : null,
              ['Language', claim.language],
              ['Check-worthiness', `${pct(claim.check_worthiness)}${claim.worthiness_reason ? `: ${claim.worthiness_reason}` : ''}`],
              claim.claims.length ? ['Claims found', claim.claims.map((c) => `${c.id}: ${c.text}`).join('\n')] : null,
              claim.subject.is_identifiable_individual ? ['Subject', `${claim.subject.name ?? 'identifiable person'}${claim.subject.is_private_individual === false ? ' (public figure)' : ' (private or unknown: extra caution)'}`] : null,
              input?.fetch_methods.length ? ['Fetched via', input.fetch_methods.join(', ')] : null,
            ]}
            className="whitespace-pre-line"
          />
        </Disclosure>
      ) : null}
    </div>
  )
}

function SectionNav({ job }: { job: JobView }) {
  const items: Array<[string, string]> = [['#verdict', 'Verdict']]
  if (job.verdict !== 'NOT_CHECKABLE') items.push(['#evidence', 'Evidence'], ['#forensics', 'Forensics'], ['#provenance', 'Provenance'])
  if (job.fusion || job.debate) items.push(['#reasoning', 'Reasoning'])
  items.push(['#trace', 'Trace'], ['#act', 'Act'], ['#audit', 'Audit'])
  return (
    <nav aria-label="On this page" className="-mx-1 flex gap-1 overflow-x-auto pb-1">
      {items.map(([href, label]) => (
        <a key={href} href={href} className="shrink-0 rounded-full border border-border bg-surface px-3 py-1.5 text-sm font-semibold text-fg no-underline hover:border-border-strong">
          {label}
        </a>
      ))}
    </nav>
  )
}

function AuditCard({ job }: { job: JobView }) {
  return (
    <Section id="audit" title="Tamper-evident audit record" icon="chain" subtitle="Every run is written to a hash-chained, Ed25519-signed log, including abstains and failures.">
      {job.audit_id != null ? (
        <div className="flex flex-wrap items-center gap-3">
          <span className="chip">entry #{job.audit_id}</span>
          <Link to={`/a/${encodeURIComponent(job.job_id)}`} className="btn btn-primary">
            <Icon name="shield" size={18} /> Open audit record & verify chain
          </Link>
          {MOCK ? null : (
            <a href={links.auditPdf(job.job_id)} className="btn btn-secondary">
              <Icon name="download" size={18} /> Signed PDF
            </a>
          )}
        </div>
      ) : (
        <p className="text-sm text-muted">The audit entry is written when the run finishes.</p>
      )}
      {Object.keys(job.resolved_models).length ? (
        <p className="mt-3 text-xs text-muted">
          Models used:{' '}
          {Object.entries(job.resolved_models)
            .map(([k, v]) => `${k} ${v}`)
            .join(' · ')}
        </p>
      ) : null}
    </Section>
  )
}

export default function JobPage() {
  const { id = '' } = useParams()
  const { job, error, notFound, connection, live, refresh } = useJob(id)
  const vis = job ? verdictVisual(job) : null
  usePageTitle(vis && job?.verdict ? `${vis.headline}` : 'Checking…')
  const evidence = useMemo(() => (job ? allEvidence(job) : []), [job])
  const sourceIndex = useMemo(() => new Map(evidence.map((e) => [e.id, e])), [evidence])

  if (notFound) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">Check not found</h1>
        <Callout tone="neutral" title={`No job with id ${id}`}>
          {error} The link may be mistyped, or the record may have expired.
        </Callout>
        <Link to="/" className="btn btn-primary">
          Start a new check
        </Link>
      </div>
    )
  }

  if (!job) {
    return (
      <div className="space-y-4">
        <h1 className="sr-only">Verification</h1>
        {error ? (
          <Callout tone="danger" role="alert" title="Could not load this check">
            {error}{' '}
            <button type="button" className="underline" onClick={refresh}>
              Retry
            </button>
          </Callout>
        ) : (
          <Spinner label="Loading…" />
        )}
        <TraceTimeline job={null} live={live} connection={connection} />
      </div>
    )
  }

  const terminal = isTerminalStatus(job.status)
  const done = terminal && !!job.verdict
  const checkable = job.verdict !== 'NOT_CHECKABLE'

  return (
    <div className="space-y-5">
      <h1 className="sr-only">Verification result</h1>
      <InputHeader job={job} />
      {error && !terminal ? <p className="text-sm text-muted">Connection hiccup: {error}. Retrying…</p> : null}
      <StatusPanel job={job} />

      {done ? <SectionNav job={job} /> : null}

      <div id="verdict" className="scroll-mt-24" aria-live="polite">
        {done ? <VerdictCard job={job} sourceIndex={sourceIndex} /> : null}
      </div>

      <TraceTimeline job={job} live={live} connection={connection} />

      {done && checkable ? (
        <>
          <EvidenceList items={evidence} citedIds={job.cited_evidence_ids} evidenceState={job.evidence_state} independentCount={job.independent_source_count} />
          <ForensicPanel forensic={job.forensic} input={job.input} />
          <ProvenancePanel provenance={job.provenance} />
        </>
      ) : null}
      {done ? <ReasoningPanel fusion={job.fusion} debate={job.debate} sourceIndex={sourceIndex} /> : null}

      {done ? (
        <Section id="act" title="Take action" icon="flag" subtitle="Close the loop: evidence bundle → platform report → legal escalation. Every step needs your confirmation.">
          <div className="space-y-6">
            {job.report_eligible ? (
              <ReportFlow job={job} />
            ) : (
              <p className="flex items-start gap-2 text-sm text-muted">
                <Icon name="info" size={16} className="mt-0.5" />
                Platform reports are prepared only for FALSE or MANIPULATED verdicts. An abstain is never reported.
              </p>
            )}
            {job.legal_eligible || job.legal_block_reason ? (
              <div className="border-t border-border pt-5">
                <LegalWizard job={job} />
              </div>
            ) : null}
          </div>
        </Section>
      ) : null}

      {terminal ? <AuditCard job={job} /> : null}
    </div>
  )
}
