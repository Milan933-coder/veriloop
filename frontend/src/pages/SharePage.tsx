import { useId, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import { Icon } from '../components/Icon'
import { Callout } from '../components/ui'
import { usePageTitle } from '../hooks/usePageTitle'
import { LOCALES, preferredLocale } from '../lib/locales'
import { PLATFORM_LABELS } from '../lib/platform'
import { SHARED_TEXT_MIN_CHARS, parseShareParams } from '../lib/share'

/**
 * PWA share target (manifest share_target -> GET /share?title=&text=&url=).
 * Shows what was shared and submits after ONE confirmation tap.
 */
export default function SharePage() {
  usePageTitle('Shared to VeriLoop')
  const location = useLocation()
  const navigate = useNavigate()
  const ids = useId()
  const shared = useMemo(() => parseShareParams(new URLSearchParams(location.search)), [location.search])
  const [includeText, setIncludeText] = useState(() => !shared.url || (shared.text?.length ?? 0) >= SHARED_TEXT_MIN_CHARS)
  const [locale, setLocale] = useState(() => preferredLocale(typeof navigator === 'undefined' ? undefined : navigator.languages))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const sendText = shared.text && (includeText || !shared.url) ? shared.text : null
  const usable = !!shared.url || !!shared.text

  const verify = async () => {
    if (!usable || busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.submit({
        url: shared.url,
        text: sendText,
        platform: shared.url ? shared.platform : 'text',
        locale,
        source: 'pwa',
      })
      navigate(`/j/${encodeURIComponent(res.job_id)}`, { replace: true })
    } catch (e) {
      setError(errorMessage(e))
      setBusy(false)
    }
  }

  if (!usable) {
    return (
      <div className="mx-auto max-w-xl space-y-4">
        <h1 className="text-2xl font-bold">Nothing to check</h1>
        <Callout tone="neutral" title="The share did not include a link or text">
          Try sharing the post itself (not a screenshot) or copy its link and paste it on the home page.
        </Callout>
        <Link to="/" className="btn btn-primary">
          Open VeriLoop
        </Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-xl space-y-5">
      <div>
        <p className="eyebrow">Shared to VeriLoop</p>
        <h1 className="text-2xl font-extrabold">Check this?</h1>
      </div>
      <div className="card space-y-4 p-4">
        {shared.url ? (
          <div>
            <p className="mb-1 flex flex-wrap items-center gap-2 text-sm font-semibold">
              Link {shared.platform ? <span className="chip">{PLATFORM_LABELS[shared.platform]}</span> : null}
            </p>
            <p className="break-all rounded-md bg-surface-2 p-2 font-mono text-sm">{shared.url}</p>
          </div>
        ) : null}
        {shared.text ? (
          <div>
            <p className="mb-1 text-sm font-semibold">{shared.url ? 'Shared text' : 'Claim to check'}</p>
            <p className="whitespace-pre-wrap break-words rounded-md bg-surface-2 p-2 text-sm">{shared.text}</p>
            {shared.url ? (
              <label className="mt-2 flex cursor-pointer items-center gap-2 text-sm">
                <input type="checkbox" className="h-4 w-4 accent-[var(--color-primary)]" checked={includeText} onChange={(e) => setIncludeText(e.target.checked)} />
                Also check this text together with the link
              </label>
            ) : null}
          </div>
        ) : null}
        <div>
          <label htmlFor={`${ids}-locale`} className="mb-1 block text-sm font-semibold">
            Language and region
          </label>
          <select id={`${ids}-locale`} className="field" value={locale} onChange={(e) => setLocale(e.target.value)}>
            {LOCALES.map((l) => (
              <option key={l.id} value={l.id}>
                {l.label}
              </option>
            ))}
          </select>
        </div>
        <button type="button" className="btn btn-primary w-full text-base" onClick={verify} disabled={busy} autoFocus>
          <Icon name={busy ? 'loader' : 'search'} size={20} />
          {busy ? 'Starting…' : 'Verify now'}
        </button>
        {error ? (
          <Callout tone="danger" role="alert" title="Could not start the check">
            {error}
          </Callout>
        ) : null}
      </div>
      <p className="text-center text-sm">
        <Link to="/">Edit before checking</Link>
      </p>
    </div>
  )
}
