import { useEffect, useId, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router'
import { api, loadMock } from '../api/client'
import { errorMessage } from '../api/errors'
import type { Platform } from '../api/types'
import { Icon, type IconName } from '../components/Icon'
import { Callout } from '../components/ui'
import { usePageTitle } from '../hooks/usePageTitle'
import { DEFAULT_LOCALE, LOCALES } from '../lib/locales'
import { MOCK, MOCK_ALLOWED } from '../lib/mockMode'
import { LINK_PLATFORMS, PLATFORM_LABELS, detectPlatform, parseHttpUrl } from '../lib/platform'
import { MAX_UPLOAD_MB, formatBytes, validateUpload } from '../lib/upload'

type Mode = 'url' | 'upload' | 'text'

const MODES: Array<{ id: Mode; label: string; icon: IconName }> = [
  { id: 'url', label: 'Link', icon: 'link' },
  { id: 'upload', label: 'Photo / video', icon: 'upload' },
  { id: 'text', label: 'Text', icon: 'text' },
]

const EXAMPLES: Array<{ title: string; format: string; mode: Mode }> = [
  { title: 'X (Twitter) post', format: 'https://x.com/<user>/status/<id>', mode: 'url' },
  { title: 'Instagram reel or post', format: 'https://www.instagram.com/reel/<id>/', mode: 'url' },
  { title: 'YouTube video or Short', format: 'https://youtu.be/<id>', mode: 'url' },
  { title: 'Facebook post or video', format: 'https://www.facebook.com/<page>/posts/<id>', mode: 'url' },
  { title: 'Telegram channel post', format: 'https://t.me/<channel>/<id>', mode: 'url' },
  { title: 'News article or any web page', format: 'https://<site>/<article>', mode: 'url' },
  { title: 'WhatsApp forward or screenshot', format: 'Paste the forwarded text, or upload the image', mode: 'text' },
  { title: 'Video clip you received', format: `Upload the file (max ${MAX_UPLOAD_MB} MB; long videos are sampled)`, mode: 'upload' },
]

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>
}

function useInstallPrompt(): (() => void) | null {
  const [evt, setEvt] = useState<BeforeInstallPromptEvent | null>(null)
  useEffect(() => {
    const on = (e: Event) => {
      e.preventDefault()
      setEvt(e as BeforeInstallPromptEvent)
    }
    window.addEventListener('beforeinstallprompt', on)
    return () => window.removeEventListener('beforeinstallprompt', on)
  }, [])
  return evt ? () => void evt.prompt().finally(() => setEvt(null)) : null
}

function MockScenarioPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [options, setOptions] = useState<Array<{ key: string; title: string }>>([])
  useEffect(() => {
    if (!(MOCK_ALLOWED && MOCK)) return
    void loadMock().then((m) => setOptions(m.SCENARIO_KEYS.map((k) => ({ key: k, title: m.SCENARIOS[k].title }))))
  }, [])
  if (!MOCK) return null
  return (
    <div className="rounded-lg border-2 border-dashed border-mock-fg bg-mock-bg p-3 text-mock-fg">
      <label htmlFor="mock-scenario" className="mb-1 block text-sm font-bold">
        Mock scenario (dev only, fictional data)
      </label>
      <select id="mock-scenario" className="field" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Auto: pick from the text you enter</option>
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.title}
          </option>
        ))}
      </select>
    </div>
  )
}

export default function SubmitPage() {
  usePageTitle('Verify a claim')
  const navigate = useNavigate()
  const ids = useId()
  const [mode, setMode] = useState<Mode>('url')
  const [url, setUrl] = useState('')
  const [platformOverride, setPlatformOverride] = useState<Platform | null>(null)
  const [text, setText] = useState('')
  const [caption, setCaption] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [locale, setLocale] = useState(DEFAULT_LOCALE)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [scenario, setScenario] = useState('')
  const install = useInstallPrompt()

  const detected = useMemo(() => detectPlatform(url), [url])
  const platform = platformOverride ?? detected
  const urlInvalid = url.trim() !== '' && !parseHttpUrl(url)
  const fileError = file ? validateUpload(file) : null

  const ready = mode === 'url' ? !!parseHttpUrl(url) : mode === 'text' ? text.trim().length >= 8 : !!file && !fileError

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!ready || busy) return
    setBusy(true)
    setError(null)
    setProgress(null)
    try {
      if (MOCK_ALLOWED && MOCK) (await loadMock()).setNextScenario(scenario || null)
      let res
      if (mode === 'upload' && file) {
        setProgress(0)
        res = await api.submitUpload(file, { locale, platform: 'upload', source: 'web', text: caption.trim() || null }, setProgress)
      } else if (mode === 'url') {
        res = await api.submit({ url: parseHttpUrl(url)!.toString(), platform: platform ?? null, locale, source: 'web' })
      } else {
        res = await api.submit({ text: text.trim(), platform: 'text', locale, source: 'web' })
      }
      navigate(`/j/${encodeURIComponent(res.job_id)}`)
    } catch (err) {
      setError(errorMessage(err))
      setBusy(false)
      setProgress(null)
    }
  }

  return (
    <div className="space-y-8">
      <div className="max-w-3xl">
        <h1 className="text-3xl font-extrabold tracking-tight sm:text-4xl">Is this real?</h1>
        <p className="mt-2 text-lg text-muted">
          Paste a link, upload a photo or video, or type a claim. VeriLoop checks it against fact-checkers, news wires,
          government releases and deepfake detectors, then gives a cited verdict or honestly says it cannot tell.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-5 p-4 sm:p-6" aria-describedby={`${ids}-hint`}>
        <fieldset>
          <legend className="sr-only">What do you want to check?</legend>
          <div className="grid grid-cols-3 gap-1 rounded-lg bg-surface-2 p-1" role="radiogroup" aria-label="Input type">
            {MODES.map((m) => (
              <label
                key={m.id}
                className={`flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-md px-2 text-sm font-semibold has-[:focus-visible]:outline has-[:focus-visible]:outline-3 has-[:focus-visible]:outline-focus ${
                  mode === m.id ? 'bg-surface text-fg shadow-sm ring-1 ring-border' : 'text-muted'
                }`}
              >
                <input type="radio" name={`${ids}-mode`} value={m.id} checked={mode === m.id} onChange={() => setMode(m.id)} className="sr-only" />
                <Icon name={m.icon} size={18} />
                <span>{m.label}</span>
              </label>
            ))}
          </div>
        </fieldset>

        {mode === 'url' ? (
          <div className="space-y-3">
            <div>
              <label htmlFor={`${ids}-url`} className="mb-1 block font-semibold">
                Link to the post, video or article
              </label>
              <input
                id={`${ids}-url`}
                type="url"
                inputMode="url"
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                className="field text-base"
                placeholder="https://www.instagram.com/reel/…"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                aria-invalid={urlInvalid || undefined}
                aria-describedby={`${ids}-url-help`}
              />
              <p id={`${ids}-url-help`} className={`mt-1 text-sm ${urlInvalid ? 'text-false-fg' : 'text-muted'}`}>
                {urlInvalid ? 'That does not look like a web link. It should start with https://' : 'Links from X, Instagram, Facebook, YouTube, TikTok, Telegram, WhatsApp or any news site.'}
              </p>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <div>
                <label htmlFor={`${ids}-platform`} className="mb-1 block text-sm font-semibold">
                  Platform {detected && !platformOverride ? <span className="font-normal text-muted">(detected from the link)</span> : null}
                </label>
                <select
                  id={`${ids}-platform`}
                  className="field w-56 max-w-full"
                  value={platform ?? ''}
                  onChange={(e) => setPlatformOverride((e.target.value || null) as Platform | null)}
                >
                  <option value="">{url ? 'Unknown' : 'Detected automatically'}</option>
                  {LINK_PLATFORMS.map((p) => (
                    <option key={p} value={p}>
                      {PLATFORM_LABELS[p]}
                    </option>
                  ))}
                </select>
              </div>
              {platformOverride && platformOverride !== detected ? (
                <button type="button" className="text-sm underline" onClick={() => setPlatformOverride(null)}>
                  Use detected ({detected ? PLATFORM_LABELS[detected] : 'none'})
                </button>
              ) : null}
            </div>
          </div>
        ) : null}

        {mode === 'upload' ? (
          <div className="space-y-3">
            <div>
              <label htmlFor={`${ids}-file`} className="mb-1 block font-semibold">
                Photo or video (max {MAX_UPLOAD_MB} MB)
              </label>
              <input
                id={`${ids}-file`}
                type="file"
                accept="image/*,video/*"
                className="field cursor-pointer py-2 file:mr-3 file:rounded-md file:border-0 file:bg-primary-soft file:px-3 file:py-1.5 file:font-semibold file:text-fg"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                aria-invalid={fileError ? true : undefined}
                aria-describedby={`${ids}-file-help`}
              />
              <p id={`${ids}-file-help`} className={`mt-1 text-sm ${fileError ? 'text-false-fg' : 'text-muted'}`}>
                {fileError ??
                  (file
                    ? `${file.name} · ${file.type || 'unknown type'} · ${formatBytes(file.size)}`
                    : 'Long videos are sampled (about 1 frame per second, capped), and that is stated in the result.')}
              </p>
            </div>
            <div>
              <label htmlFor={`${ids}-caption`} className="mb-1 block text-sm font-semibold">
                What does it claim? <span className="font-normal text-muted">(optional caption or message text)</span>
              </label>
              <textarea id={`${ids}-caption`} rows={2} className="field" value={caption} onChange={(e) => setCaption(e.target.value)} />
            </div>
          </div>
        ) : null}

        {mode === 'text' ? (
          <div>
            <label htmlFor={`${ids}-text`} className="mb-1 block font-semibold">
              The claim, as you received it
            </label>
            <textarea
              id={`${ids}-text`}
              rows={4}
              className="field text-base"
              placeholder="For example, a forwarded message: “Government announces … from Monday”"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <p className="mt-1 text-sm text-muted">Any language. Paste the full message for the best result.</p>
          </div>
        ) : null}

        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <label htmlFor={`${ids}-locale`} className="mb-1 block text-sm font-semibold">
              Language and region
            </label>
            <select id={`${ids}-locale`} className="field w-56 max-w-full" value={locale} onChange={(e) => setLocale(e.target.value)}>
              {LOCALES.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.label}
                </option>
              ))}
            </select>
          </div>
          <button type="submit" className="btn btn-primary min-w-40 text-base" disabled={!ready || busy}>
            <Icon name={busy ? 'loader' : 'search'} size={18} />
            {busy ? (progress != null && progress < 1 ? `Uploading ${Math.round(progress * 100)}%` : 'Submitting…') : 'Verify'}
          </button>
        </div>

        {progress != null ? (
          <div className="h-2 w-full overflow-hidden rounded bg-surface-2" role="progressbar" aria-label="Upload progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
            <div className="h-full bg-primary" style={{ width: `${progress * 100}%` }} />
          </div>
        ) : null}

        <MockScenarioPicker value={scenario} onChange={setScenario} />

        {error ? (
          <Callout tone="danger" role="alert" title="Could not start the check">
            {error}
          </Callout>
        ) : null}
        <p id={`${ids}-hint`} className="text-xs text-muted">
          Results are public-interest checks, not legal findings. Nothing is reported or sent anywhere without your
          explicit confirmation.
        </p>
      </form>

      <section aria-labelledby="examples-title" className="space-y-3">
        <h2 id="examples-title" className="text-lg font-bold">
          What you can paste
        </h2>
        <ul className="grid gap-2 sm:grid-cols-2">
          {EXAMPLES.map((ex) => (
            <li key={ex.title}>
              <button
                type="button"
                onClick={() => setMode(ex.mode)}
                className="flex h-full w-full items-start gap-3 rounded-lg border border-border bg-surface p-3 text-left hover:border-border-strong"
              >
                <Icon name={ex.mode === 'url' ? 'link' : ex.mode === 'upload' ? 'upload' : 'text'} size={18} className="mt-0.5 text-muted" />
                <span className="min-w-0">
                  <span className="block font-semibold">{ex.title}</span>
                  <code className="block break-all text-sm text-muted">{ex.format}</code>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section className="card flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <Icon name="share" size={22} className="mt-0.5 text-muted" />
          <p className="text-sm">
            <strong>On Android:</strong> install VeriLoop, then use <em>Share → VeriLoop</em> from Instagram, YouTube, X or
            WhatsApp to check a post in one tap.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          {install ? (
            <button type="button" className="btn btn-secondary" onClick={install}>
              Install app
            </button>
          ) : null}
          <Link to="/about" className="btn btn-secondary">
            How it works <Icon name="arrow-right" size={16} />
          </Link>
        </div>
      </section>
    </div>
  )
}
