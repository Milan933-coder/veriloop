import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'
import { api, links } from '../api/client'
import { ApiError, errorMessage } from '../api/errors'
import type { AuditEntryView, AuditVerifyResult } from '../api/types'
import { Icon } from '../components/Icon'
import { QrCode } from '../components/QrCode'
import { Callout, CopyButton, Disclosure, Section, Spinner } from '../components/ui'
import { usePageTitle } from '../hooks/usePageTitle'
import { checkPayloadHash, checkSignature, type CheckResult } from '../lib/auditCrypto'
import { formatDateTime, humanize } from '../lib/format'
import { MOCK } from '../lib/mockMode'

function HashField({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center justify-between gap-2">
        <dt className="text-sm font-semibold">{label}</dt>
        <CopyButton text={value} label="Copy" className="min-h-8 px-2" />
      </div>
      <dd className="mt-1 break-all rounded-md bg-surface-2 p-2 font-mono text-xs">{value}</dd>
      {hint ? <p className="mt-0.5 text-xs text-muted">{hint}</p> : null}
    </div>
  )
}

function VerifyResult({ result }: { result: AuditVerifyResult }) {
  if (result.ok) {
    return (
      <div role="status" className="flex items-center gap-4 rounded-xl border-2 border-true-line bg-true-soft p-4 text-true-fg sm:p-6">
        <span className="text-5xl font-black leading-none sm:text-6xl" aria-hidden="true">
          ✓
        </span>
        <div>
          <p className="text-2xl font-extrabold sm:text-3xl">ok: true. Chain intact</p>
          <p className="mt-1 text-sm">
            Recomputed {result.checked_rows} row{result.checked_rows === 1 ? '' : 's'} from genesis: every hash, link and
            signature matches.
          </p>
          {result.head_hash ? <p className="mt-1 break-all font-mono text-xs">head {result.head_hash}</p> : null}
        </div>
      </div>
    )
  }
  return (
    <div role="alert" className="flex items-center gap-4 rounded-xl border-2 border-false-line bg-false-soft p-4 text-false-fg sm:p-6">
      <span className="text-5xl font-black leading-none sm:text-6xl" aria-hidden="true">
        ✗
      </span>
      <div>
        <p className="text-2xl font-extrabold sm:text-3xl">Chain broken{result.broken_at != null ? ` at row ${result.broken_at}` : ''}</p>
        <p className="mt-1 text-sm">{result.reason ?? 'A row no longer matches its hash, link or signature.'}</p>
        <p className="mt-1 text-xs">Checked {result.checked_rows} rows.</p>
      </div>
    </div>
  )
}

const CHECK_TEXT: Record<CheckResult, { text: string; cls: string; icon: 'check-circle' | 'x-octagon' | 'help-circle' }> = {
  pass: { text: 'matches', cls: 'text-true-fg', icon: 'check-circle' },
  fail: { text: 'DOES NOT match', cls: 'text-false-fg', icon: 'x-octagon' },
  unsupported: { text: 'not supported by this browser (needs HTTPS and WebCrypto Ed25519)', cls: 'text-muted', icon: 'help-circle' },
}

function BrowserCheck({ entry }: { entry: AuditEntryView }) {
  const [state, setState] = useState<{ hash: CheckResult; sig: CheckResult; pem: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      const hash = await checkPayloadHash(entry.payload_json, entry.payload_hash)
      const pem = await api.publicKeyPem()
      const sig = await checkSignature(pem, entry.prev_hash, entry.payload_hash, entry.signature)
      setState({ hash, sig, pem })
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">Do not trust the server: your browser can recompute the hash and check the signature itself.</p>
      <button type="button" className="btn btn-secondary" onClick={run} disabled={busy}>
        <Icon name={busy ? 'loader' : 'key'} size={18} /> Check in my browser
      </button>
      {error ? <Callout tone="danger">{error}</Callout> : null}
      {state ? (
        <ul className="space-y-1 text-sm" role="status">
          {(
            [
              ['SHA-256(payload_json) vs payload_hash', state.hash],
              ['Ed25519 signature over prev_hash:payload_hash', state.sig],
            ] as const
          ).map(([label, r]) => (
            <li key={label} className={`flex items-start gap-2 ${CHECK_TEXT[r].cls}`}>
              <Icon name={CHECK_TEXT[r].icon} size={18} className="mt-0.5" />
              <span>
                <strong>{label}</strong>: {CHECK_TEXT[r].text}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {state?.pem ? (
        <Disclosure summary="Public key used">
          <pre className="whitespace-pre-wrap break-all font-mono text-xs">{state.pem}</pre>
        </Disclosure>
      ) : null}
    </div>
  )
}

export default function AuditPage() {
  const { id = '' } = useParams()
  usePageTitle('Audit record')
  const [entry, setEntry] = useState<AuditEntryView | null>(null)
  const [loadError, setLoadError] = useState<{ status: number; message: string } | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [result, setResult] = useState<AuditVerifyResult | null>(null)
  const [verifyError, setVerifyError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setEntry(null)
    setLoadError(null)
    api
      .audit(id)
      .then((e) => alive && setEntry(e))
      .catch((e: unknown) => alive && setLoadError({ status: e instanceof ApiError ? e.status : 0, message: errorMessage(e) }))
    return () => {
      alive = false
    }
  }, [id])

  const verify = async () => {
    setVerifying(true)
    setVerifyError(null)
    setResult(null)
    try {
      setResult(await api.auditVerify(id))
    } catch (e) {
      setVerifyError(errorMessage(e))
    } finally {
      setVerifying(false)
    }
  }

  const pretty = useMemo(() => {
    if (!entry) return ''
    try {
      return JSON.stringify(JSON.parse(entry.payload_json), null, 2)
    } catch {
      return entry.payload_json
    }
  }, [entry])

  const pageUrl = typeof window === 'undefined' ? '' : `${window.location.origin}${window.location.pathname}`

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Link to={`/j/${encodeURIComponent(id)}`} className="inline-flex items-center gap-1 font-semibold">
          <Icon name="arrow-right" size={16} className="rotate-180" /> Back to the result
        </Link>
      </div>
      <div>
        <p className="eyebrow">Tamper-evident audit</p>
        <h1 className="text-2xl font-extrabold sm:text-3xl">Audit record{entry ? ` #${entry.id}` : ''}</h1>
        <p className="mt-1 text-muted">
          Each entry stores the SHA-256 of its canonical payload and the previous entry's hash, and is signed with the
          server's Ed25519 key. Editing any past row breaks every link after it.
        </p>
      </div>

      <section aria-labelledby="verify-title" className="card space-y-4 p-4 sm:p-6">
        <h2 id="verify-title" className="text-lg font-bold">
          Verify the whole chain
        </h2>
        <button type="button" className="btn btn-primary w-full py-4 text-lg sm:w-auto sm:px-8" onClick={verify} disabled={verifying}>
          <Icon name={verifying ? 'loader' : 'shield'} size={24} />
          {verifying ? 'Recomputing the chain…' : 'Verify chain'}
        </button>
        <div aria-live="polite">
          {result ? <VerifyResult result={result} /> : null}
          {verifyError ? (
            <Callout tone="danger" role="alert" title="Verification request failed">
              {verifyError}
            </Callout>
          ) : null}
        </div>
        {result?.job_entry_ids.length ? (
          <p className="text-sm text-muted">Entries for this job: {result.job_entry_ids.map((n) => `#${n}`).join(', ')}</p>
        ) : null}
      </section>

      {loadError ? (
        <Callout tone={loadError.status === 404 || loadError.status === 409 ? 'neutral' : 'danger'} title="No audit entry to show yet">
          {loadError.message}. The entry is written when the run finishes; check the <Link to={`/j/${encodeURIComponent(id)}`}>result page</Link>.
        </Callout>
      ) : !entry ? (
        <Spinner label="Loading audit entry…" />
      ) : (
        <>
          <Section id="entry" title="Chain entry" icon="chain" subtitle={`${humanize(entry.event_type)} · ${formatDateTime(entry.created_at)} · job ${entry.job_id}`}>
            <dl className="grid gap-4 md:grid-cols-2">
              <HashField label="prev_hash" value={entry.prev_hash} hint="payload_hash of the previous row (genesis = 64 zeros)" />
              <HashField label="payload_hash" value={entry.payload_hash} hint="SHA-256 of the canonical payload_json below" />
              <HashField label="signature (Ed25519, base64)" value={entry.signature} hint="over “prev_hash:payload_hash”" />
              <HashField label="key fingerprint" value={entry.key_fingerprint} />
            </dl>
            <div className="mt-5 grid gap-4 md:grid-cols-2">
              <div>
                <h3 className="eyebrow mb-1">Resolved model ids</h3>
                <ul className="space-y-0.5 text-sm">
                  {Object.entries(entry.resolved_model_ids).map(([k, v]) => (
                    <li key={k} className="break-all">
                      {k}: <code>{v}</code>
                    </li>
                  ))}
                  {Object.keys(entry.resolved_model_ids).length === 0 ? <li className="text-muted">none recorded</li> : null}
                </ul>
              </div>
              <div>
                <h3 className="eyebrow mb-1">Tool versions</h3>
                <ul className="space-y-0.5 text-sm">
                  {Object.entries(entry.tool_versions).map(([k, v]) => (
                    <li key={k} className="break-all">
                      {k}: <code>{v}</code>
                    </li>
                  ))}
                  {Object.keys(entry.tool_versions).length === 0 ? <li className="text-muted">none recorded</li> : null}
                </ul>
              </div>
            </div>
            <Disclosure className="mt-5" summary="payload_json (exactly what was hashed, shown formatted)">
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all font-mono text-xs">{pretty}</pre>
            </Disclosure>
          </Section>

          <Section id="browser-check" title="Independent check" icon="key">
            <BrowserCheck entry={entry} />
          </Section>
        </>
      )}

      <Section id="downloads" title="Downloads" icon="download">
        <div className="flex flex-col gap-5 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex flex-wrap gap-2">
            {MOCK ? (
              <p className="text-sm text-muted">Mock mode: the signed PDF and public key file are served by the real backend.</p>
            ) : (
              <>
                <a href={links.auditPdf(id)} className="btn btn-primary">
                  <Icon name="download" size={18} /> Signed PDF
                </a>
                <a href={links.publicKey()} className="btn btn-secondary">
                  <Icon name="key" size={18} /> Public key (PEM)
                </a>
              </>
            )}
          </div>
          {pageUrl ? (
            <figure className="flex items-center gap-3">
              <QrCode value={pageUrl} size={132} label="QR code linking to this audit page" />
              <figcaption className="max-w-[12rem] text-sm text-muted">Scan to open this audit record and verify it on your own phone.</figcaption>
            </figure>
          ) : null}
        </div>
      </Section>
    </div>
  )
}
