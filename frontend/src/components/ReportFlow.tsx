import { useId, useState } from 'react'
import { api, links } from '../api/client'
import { errorMessage } from '../api/errors'
import type { JobView, PlatformPlan, ReportConfirmResponse, ReportDraftPackage, ReportMethod } from '../api/types'
import { MOCK } from '../lib/mockMode'
import { PLATFORM_LABELS } from '../lib/platform'
import { Icon } from './Icon'
import { Callout, CopyButton, ExternalLink } from './ui'

const METHOD_TEXT: Record<ReportMethod, string> = {
  playwright_guided: 'Guided browser report (pre-filled, you submit)',
  report_card: 'Report card image + text (no platform API)',
  deeplink_guided: 'Link to the platform report page (you submit)',
}

export const REPORT_GUIDANCE: Record<ReportMethod, string> = {
  playwright_guided: "The reporter agent will open the platform's report form pre-filled; you press submit.",
  report_card: 'Download the report card and share it together with the text above. This platform has no report API, so nothing is sent by VeriLoop.',
  deeplink_guided: "Open the platform's report page; paste the prepared text and press submit yourself.",
}

function platformName(p: string): string {
  return (PLATFORM_LABELS as Record<string, string | undefined>)[p] ?? p
}

function BundleContents({ pkg, jobId }: { pkg: ReportDraftPackage; jobId: string }) {
  const b = pkg.bundle
  if (!b) return null
  // File names follow SPEC §7; the ZIP is built by the server from these same fields.
  const files: Array<[string, string]> = [
    ['verdict.json', `label, confidence, rationale${typeof b.verdict.label === 'string' ? ` (${b.verdict.label})` : ''}`],
    ['sources.json', `${b.sources.length} cited source${b.sources.length === 1 ? '' : 's'} with URLs and retrieval method`],
  ]
  if (b.forensic_report) files.push(['forensic_report.json', 'detector scores and ensemble'])
  files.push(['media hashes', `${b.media_hashes.length} SHA-256 + pHash entr${b.media_hashes.length === 1 ? 'y' : 'ies'}`])
  if (Object.keys(b.timestamps).length) files.push(['timestamps', Object.keys(b.timestamps).join(', ')])
  if (b.audit_id != null) files.push(['audit id', `#${b.audit_id}`])
  const zip = pkg.bundle_url ?? links.bundleZip(jobId)
  return (
    <div>
      <h4 className="mb-2 font-semibold">Evidence bundle</h4>
      <ul className="divide-y divide-border rounded-lg border border-border text-sm">
        {files.map(([name, desc]) => (
          <li key={name} className="flex flex-wrap items-baseline gap-x-3 px-3 py-1.5">
            <code className="font-semibold">{name}</code>
            <span className="text-muted">{desc}</span>
          </li>
        ))}
        <li className="flex flex-wrap items-baseline gap-x-3 px-3 py-1.5">
          <span className="font-semibold">Signed audit PDF</span>
          <a href={b.pdf_url} className="break-all">
            {b.pdf_url}
          </a>
        </li>
        <li className="flex flex-wrap items-baseline gap-x-3 px-3 py-1.5">
          <span className="font-semibold">Verify link</span>
          <a href={b.verify_url} className="break-all">
            {b.verify_url}
          </a>
        </li>
      </ul>
      {MOCK ? (
        <p className="mt-2 text-sm text-muted">Mock mode: the ZIP download is served by the real backend only.</p>
      ) : (
        <a href={zip} download className="btn btn-secondary mt-2">
          <Icon name="download" size={18} /> Download bundle.zip
        </a>
      )}
    </div>
  )
}

function ReportCard({ plan, jobId }: { plan: PlatformPlan | undefined; jobId: string }) {
  if (MOCK) {
    return (
      <div className="flex aspect-[1.91/1] max-w-md items-center justify-center rounded-lg border-2 border-dashed border-border-strong bg-surface-2 p-4 text-center text-sm text-muted">
        MOCK: the report-card PNG (verdict, sources, QR to the audit PDF) is rendered by the backend at /report/{'{id}'}/card.png
      </div>
    )
  }
  const src = plan?.card_url ?? links.reportCard(jobId)
  return (
    <a href={src} target="_blank" rel="noopener noreferrer" className="block max-w-md">
      <img src={src} alt="Report card: verdict, cited sources and a QR code linking to the signed audit PDF" className="w-full rounded-lg border border-border" loading="lazy" />
    </a>
  )
}

export function ReportFlow({ job }: { job: JobView }) {
  const [pkg, setPkg] = useState<ReportDraftPackage | null>(null)
  const [busy, setBusy] = useState<'prepare' | 'confirm' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [attempts, setAttempts] = useState<ReportConfirmResponse[]>([])
  const groupName = useId()

  const prepare = async () => {
    setBusy('prepare')
    setError(null)
    try {
      const p = await api.prepareReport(job.job_id)
      setPkg(p)
      setSelected(p.platform_plans[0]?.platform ?? null)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const confirm = async () => {
    if (!selected || !confirmed) return
    setBusy('confirm')
    setError(null)
    try {
      const res = await api.confirmReport(job.job_id, { platform: selected, confirmed: true })
      setAttempts((a) => [...a.filter((x) => x.platform !== res.platform), res])
      setConfirmed(false)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }

  const plan = pkg?.platform_plans.find((p) => p.platform === selected)
  const cardPlan = pkg?.platform_plans.find((p) => p.method === 'report_card') ?? plan

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <Icon name="flag" size={24} className="mt-0.5 text-muted" />
        <div>
          <h3 className="text-base font-bold">Report this to the platform</h3>
          <p className="text-sm text-muted">
            Step 1: prepare the package. Step 2: review exactly what would be sent. Step 3: confirm. VeriLoop never
            submits a report by itself; you press the final submit button.
          </p>
        </div>
      </div>

      {!pkg ? (
        <button type="button" className="btn btn-primary" onClick={prepare} disabled={busy !== null}>
          <Icon name={busy === 'prepare' ? 'loader' : 'file'} size={18} />
          {busy === 'prepare' ? 'Preparing…' : 'Prepare report'}
        </button>
      ) : null}

      {error ? <Callout tone="danger" role="alert" title="Could not complete the request">{error}</Callout> : null}

      {pkg && !pkg.eligible ? <Callout tone="neutral" title="No report needed">{pkg.ineligible_reason ?? 'This verdict is not eligible for reporting.'}</Callout> : null}

      {pkg && pkg.eligible ? (
        <div className="space-y-5">
          <div>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h4 className="font-semibold">Exactly what will be sent</h4>
              <CopyButton text={pkg.preview_text} label="Copy text" />
            </div>
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-surface-2 p-3 text-sm" aria-label="Report text preview">
              {pkg.preview_text}
            </pre>
          </div>

          <div className="grid gap-5 md:grid-cols-2">
            <div>
              <h4 className="mb-2 font-semibold">Report card image</h4>
              <ReportCard plan={cardPlan} jobId={job.job_id} />
            </div>
            <BundleContents pkg={pkg} jobId={job.job_id} />
          </div>

          <fieldset className="space-y-2">
            <legend className="mb-1 font-semibold">Where to report</legend>
            {pkg.platform_plans.map((p) => (
              <div
                key={p.platform}
                className={`rounded-lg border p-3 ${selected === p.platform ? 'border-primary bg-primary-soft' : 'border-border bg-surface'}`}
              >
                <label className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1">
                  <input
                    type="radio"
                    name={groupName}
                    className="h-4 w-4 accent-[var(--color-primary)]"
                    checked={selected === p.platform}
                    onChange={() => {
                      setSelected(p.platform)
                      setConfirmed(false)
                    }}
                  />
                  <strong>{platformName(p.platform)}</strong>
                  <span className="chip whitespace-normal">{METHOD_TEXT[p.method] ?? p.method}</span>
                  {p.evidence_attached ? <span className="chip">evidence attached</span> : null}
                  {attempts.some((a) => a.platform === p.platform) ? (
                    <span className="chip border-true-line bg-true-soft text-true-fg">
                      <Icon name="check-circle" size={14} /> confirmed
                    </span>
                  ) : null}
                </label>
                <div className="pl-7">
                  {p.report_category ? <p className="mt-1 text-sm">Category: {p.report_category}</p> : null}
                  {p.target_url ? (
                    <p className="mt-1 text-sm">
                      Target: <ExternalLink href={p.target_url}>{p.target_url}</ExternalLink>
                    </p>
                  ) : null}
                  {p.steps.length ? (
                    <ol className="mt-2 list-decimal space-y-0.5 pl-5 text-sm">
                      {p.steps.map((s, i) => (
                        <li key={i}>{s}</li>
                      ))}
                    </ol>
                  ) : null}
                  {p.prefilled_text && p.prefilled_text !== pkg.preview_text ? (
                    <div className="mt-2">
                      <p className="text-sm font-semibold">Pre-filled text for this platform:</p>
                      <pre className="mt-1 whitespace-pre-wrap break-words rounded border border-border bg-surface-2 p-2 text-xs">{p.prefilled_text}</pre>
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
          </fieldset>

          {plan ? (
            <div className="rounded-lg border-2 border-primary/50 p-3">
              <label className="flex cursor-pointer items-start gap-3">
                <input type="checkbox" className="mt-1 h-5 w-5 accent-[var(--color-primary)]" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
                <span className="text-sm">
                  <strong>I confirm</strong> I have reviewed exactly what will be sent to {platformName(plan.platform)} and I
                  want to report this content myself. I understand nothing is submitted until I press submit on the
                  platform.
                </span>
              </label>
              <button type="button" className="btn btn-primary mt-3" disabled={!confirmed || busy !== null} onClick={confirm}>
                <Icon name={busy === 'confirm' ? 'loader' : 'check-circle'} size={18} />
                {busy === 'confirm' ? 'Logging…' : `I confirm: prepare the ${platformName(plan.platform)} report`}
              </button>
            </div>
          ) : null}

          {attempts.map((a) => (
            <Callout key={a.attempt_id} tone="success" title={`Report attempt logged for ${platformName(a.platform)}`} role="status">
              <p>{REPORT_GUIDANCE[a.method] ?? REPORT_GUIDANCE.playwright_guided}</p>
              <p className="mt-1 text-xs">
                Attempt <code>{a.attempt_id}</code>
                {a.audit_id != null ? ` · audit entry #${a.audit_id}` : ''} · logged without your identity
              </p>
              {a.reporter_agent_command ? (
                <div className="mt-2">
                  <p className="text-xs font-semibold">Presenter laptop command (reporter agent):</p>
                  <div className="mt-1 flex flex-wrap items-center gap-2">
                    <code className="break-all rounded bg-surface px-2 py-1 text-xs text-fg">{a.reporter_agent_command}</code>
                    <CopyButton text={a.reporter_agent_command} />
                  </div>
                </div>
              ) : null}
            </Callout>
          ))}
        </div>
      ) : null}
    </div>
  )
}
