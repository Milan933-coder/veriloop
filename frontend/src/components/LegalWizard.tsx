import { useId, useState } from 'react'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { JobView, Jurisdiction, LegalAcknowledgements, LegalDraftResponse } from '../api/types'
import { LINK_PLATFORMS, PLATFORM_LABELS } from '../lib/platform'
import { splitPlaceholders } from '../lib/placeholders'
import { Icon } from './Icon'
import { Callout, CopyButton } from './ui'

export const JURISDICTIONS: Array<{ id: Jurisdiction; name: string; detail: string }> = [
  { id: 'IN', name: 'India', detail: 'IT Rules 2021 complaint to the platform grievance officer (impersonation / morphed content) + DPDP Act erasure request' },
  { id: 'EU', name: 'European Union', detail: 'GDPR Article 17 right-to-erasure request' },
  { id: 'US', name: 'United States', detail: 'DMCA takedown notice (only if you own the copyright) + note on state synthetic-media / NCII statutes' },
]

/** Copy for each required acknowledgement (LegalAcknowledgements in contracts/api.py). */
export const ACKNOWLEDGEMENTS: Array<{ key: keyof LegalAcknowledgements; text: string }> = [
  { key: 'is_subject_or_authorized', text: 'I am the person shown in this media, or I am authorised to act on their behalf.' },
  { key: 'not_legal_advice', text: 'I understand this draft is generated from a template and is not legal advice.' },
  { key: 'no_auto_send', text: 'I understand VeriLoop will never send this notice. Only I can file it.' },
  { key: 'will_review_before_filing', text: 'I will review the draft and complete every placeholder before filing it anywhere.' },
]

const NONE: LegalAcknowledgements = {
  is_subject_or_authorized: false,
  not_legal_advice: false,
  no_auto_send: false,
  will_review_before_filing: false,
}

export function NotLegalAdviceBanner() {
  return (
    <div role="note" className="flex items-center gap-3 rounded-lg border-2 border-abstain-line bg-abstain-soft p-3 text-abstain-fg">
      <Icon name="lock" size={24} />
      <p className="text-base font-extrabold">Not legal advice · VeriLoop never sends this</p>
    </div>
  )
}

function DraftView({ draft }: { draft: LegalDraftResponse }) {
  const parts = splitPlaceholders(draft.draft_text, draft.placeholders)
  const count = parts.filter((p) => p.placeholder).length
  return (
    <div className="space-y-4">
      <NotLegalAdviceBanner />
      {draft.disclaimer ? <p className="text-sm text-muted">{draft.disclaimer}</p> : null}
      <div>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h4 className="font-semibold">
            Draft ({draft.jurisdiction}) · {count} placeholder{count === 1 ? '' : 's'} to fill
          </h4>
          <CopyButton text={draft.draft_text} label="Copy draft" />
        </div>
        <pre
          className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-surface-2 p-3 font-mono text-sm leading-relaxed"
          aria-label="Legal draft text (read-only)"
          aria-readonly="true"
          tabIndex={0}
        >
          {parts.map((p, i) =>
            p.placeholder ? (
              <mark key={i} className="rounded px-0.5 font-bold outline outline-1 outline-abstain-line">
                {p.text}
              </mark>
            ) : (
              <span key={i}>{p.text}</span>
            ),
          )}
        </pre>
        <p className="mt-1 text-xs text-muted">Highlighted text marks placeholders you must replace. Template {draft.template_version}.</p>
      </div>
      {draft.checklist.length ? (
        <div>
          <h4 className="mb-1 font-semibold">Before you file</h4>
          <ul className="space-y-1 text-sm">
            {draft.checklist.map((c) => (
              <li key={c} className="flex gap-2">
                <span className="mt-1 inline-block h-3.5 w-3.5 shrink-0 rounded-sm border-2 border-border-strong" aria-hidden="true" />
                {c}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {draft.filing_instructions.length ? (
        <div>
          <h4 className="mb-1 font-semibold">How to file it yourself</h4>
          <ol className="list-decimal space-y-1 pl-5 text-sm">
            {draft.filing_instructions.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ol>
        </div>
      ) : null}
      <p className="text-xs text-muted">
        Evidence reference: {draft.evidence_bundle_ref}
        {draft.audit_id != null ? ` · draft generation logged as audit entry #${draft.audit_id}` : ''}
      </p>
    </div>
  )
}

export function LegalWizard({ job }: { job: JobView }) {
  const [jurisdiction, setJurisdiction] = useState<Jurisdiction | null>(null)
  const [platform, setPlatform] = useState<string>(job.input?.platform && LINK_PLATFORMS.includes(job.input.platform) ? job.input.platform : '')
  const [acks, setAcks] = useState<LegalAcknowledgements>(NONE)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<LegalDraftResponse | null>(null)
  const ids = useId()

  if (!job.legal_eligible) {
    return (
      <div className="flex items-start gap-3">
        <Icon name="gavel" size={24} className="mt-0.5 text-muted" />
        <div>
          <h3 className="text-base font-bold">Legal escalation not available</h3>
          <p className="text-sm text-muted">
            {job.legal_block_reason ?? 'Legal drafts are only offered for MANIPULATED media of an identifiable person.'}
          </p>
        </div>
      </div>
    )
  }

  const allAck = Object.values(acks).every(Boolean)
  const canGenerate = !!jurisdiction && allAck && !busy

  const generate = async () => {
    if (!jurisdiction || !allAck) return
    setBusy(true)
    setError(null)
    try {
      setDraft(await api.legalDraft(job.job_id, { jurisdiction, acknowledgements: acks, platform: platform || null }))
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <Icon name="gavel" size={24} className="mt-0.5 text-muted" />
        <div>
          <h3 className="text-base font-bold">Legal escalation draft (for the person depicted)</h3>
          <p className="text-sm text-muted">
            Generates a jurisdiction-specific draft you can review, complete and file yourself. There is no send button.
          </p>
        </div>
      </div>
      {draft ? null : <NotLegalAdviceBanner />}

      {draft ? (
        <>
          <DraftView draft={draft} />
          <button type="button" className="btn btn-secondary" onClick={() => setDraft(null)}>
            Change jurisdiction or answers
          </button>
        </>
      ) : (
        <>
          <fieldset>
            <legend className="mb-2 font-semibold">1. Where will you file it?</legend>
            <div className="grid gap-2 sm:grid-cols-3">
              {JURISDICTIONS.map((j) => (
                <label
                  key={j.id}
                  className={`cursor-pointer rounded-lg border p-3 text-sm ${jurisdiction === j.id ? 'border-primary bg-primary-soft' : 'border-border'}`}
                >
                  <span className="flex items-center gap-2 font-bold">
                    <input
                      type="radio"
                      name={`${ids}-jur`}
                      className="h-4 w-4 accent-[var(--color-primary)]"
                      checked={jurisdiction === j.id}
                      onChange={() => setJurisdiction(j.id)}
                    />
                    {j.name}
                  </span>
                  <span className="mt-1 block text-muted">{j.detail}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div>
            <label htmlFor={`${ids}-platform`} className="mb-1 block font-semibold">
              2. Platform hosting the content <span className="font-normal text-muted">(optional)</span>
            </label>
            <select id={`${ids}-platform`} className="field max-w-xs" value={platform} onChange={(e) => setPlatform(e.target.value)}>
              <option value="">Not specified</option>
              {LINK_PLATFORMS.map((p) => (
                <option key={p} value={p}>
                  {PLATFORM_LABELS[p]}
                </option>
              ))}
            </select>
          </div>

          <fieldset>
            <legend className="mb-2 font-semibold">3. Required acknowledgements</legend>
            <div className="space-y-2">
              {ACKNOWLEDGEMENTS.map((a) => (
                <label key={a.key} className="flex cursor-pointer items-start gap-3 rounded-lg border border-border p-2.5 text-sm">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-5 w-5 accent-[var(--color-primary)]"
                    checked={acks[a.key]}
                    onChange={(e) => setAcks((s) => ({ ...s, [a.key]: e.target.checked }))}
                  />
                  {a.text}
                </label>
              ))}
            </div>
          </fieldset>

          {error ? <Callout tone="danger" role="alert" title="Could not generate the draft">{error}</Callout> : null}
          <button type="button" className="btn btn-primary" disabled={!canGenerate} onClick={generate}>
            <Icon name={busy ? 'loader' : 'file'} size={18} />
            {busy ? 'Generating…' : 'Generate draft'}
          </button>
          {!canGenerate && !busy ? (
            <p className="text-xs text-muted">Choose a jurisdiction and tick all four acknowledgements to continue.</p>
          ) : null}
        </>
      )}
    </div>
  )
}
