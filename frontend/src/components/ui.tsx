import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon, type IconName } from './Icon'

export function Section({
  id,
  title,
  icon,
  subtitle,
  actions,
  children,
  className = '',
}: {
  id?: string
  title: string
  icon?: IconName
  subtitle?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  const headingId = id ? `${id}-title` : undefined
  return (
    <section id={id} aria-labelledby={headingId} className={`card scroll-mt-24 p-4 sm:p-5 ${className}`}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 id={headingId} className="flex items-center gap-2 text-lg font-bold">
            {icon ? <Icon name={icon} className="text-muted" /> : null}
            {title}
          </h2>
          {subtitle ? <p className="mt-0.5 text-sm text-muted">{subtitle}</p> : null}
        </div>
        {actions}
      </div>
      {children}
    </section>
  )
}

type CalloutTone = 'info' | 'warn' | 'danger' | 'success' | 'neutral'
const CALLOUT: Record<CalloutTone, { box: string; icon: IconName }> = {
  info: { box: 'border-primary/40 bg-primary-soft text-fg', icon: 'info' },
  warn: { box: 'border-abstain-line bg-abstain-soft text-abstain-fg', icon: 'alert' },
  danger: { box: 'border-false-line bg-false-soft text-false-fg', icon: 'alert' },
  success: { box: 'border-true-line bg-true-soft text-true-fg', icon: 'check-circle' },
  neutral: { box: 'border-border bg-surface-2 text-fg', icon: 'info' },
}

export function Callout({
  tone = 'info',
  title,
  icon,
  children,
  className = '',
  role,
}: {
  tone?: CalloutTone
  title?: ReactNode
  icon?: IconName
  children?: ReactNode
  className?: string
  role?: 'status' | 'alert' | 'note'
}) {
  const c = CALLOUT[tone]
  return (
    <div role={role} className={`flex gap-3 rounded-lg border p-3 text-sm ${c.box} ${className}`}>
      <Icon name={icon ?? c.icon} className="mt-0.5" />
      <div className="min-w-0 flex-1 space-y-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children ? <div className="break-words">{children}</div> : null}
      </div>
    </div>
  )
}

export function ExternalLink({ href, children, className = '' }: { href: string; children: ReactNode; className?: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className={`inline-flex min-w-0 items-center gap-1 break-all underline decoration-1 underline-offset-2 ${className}`}
    >
      <span className="min-w-0 break-all">{children}</span>
      <Icon name="external-link" size={14} />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  )
}

export function CopyButton({ text, label = 'Copy', className = '' }: { text: string; label?: string; className?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setState('copied')
    } catch {
      setState('failed')
    }
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setState('idle'), 2000)
  }
  return (
    <button type="button" onClick={copy} className={`btn btn-secondary min-h-9 px-3 py-1 text-sm ${className}`}>
      <Icon name={state === 'copied' ? 'check-circle' : 'copy'} size={16} />
      <span aria-live="polite">{state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : label}</span>
    </button>
  )
}

export function Disclosure({
  summary,
  children,
  defaultOpen = false,
  className = '',
}: {
  summary: ReactNode
  children: ReactNode
  defaultOpen?: boolean
  className?: string
}) {
  return (
    <details className={`group rounded-lg border border-border bg-surface ${className}`} open={defaultOpen}>
      <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-sm font-semibold [&::-webkit-details-marker]:hidden">
        <span className="min-w-0">{summary}</span>
        <Icon name="chevron-down" size={18} className="transition-transform group-open:rotate-180" />
      </summary>
      <div className="border-t border-border px-3 py-3 text-sm">{children}</div>
    </details>
  )
}

export function KeyValue({ items, className = '' }: { items: Array<[ReactNode, ReactNode] | null | false>; className?: string }) {
  return (
    <dl className={`grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-[max-content_minmax(0,1fr)] ${className}`}>
      {items.filter(Boolean).map((it, i) => {
        const [k, v] = it as [ReactNode, ReactNode]
        return (
          <div key={i} className="contents">
            <dt className="font-semibold text-muted">{k}</dt>
            <dd className="min-w-0 break-words">{v}</dd>
          </div>
        )
      })}
    </dl>
  )
}

/** Horizontal 0..1 bar with text value; role=meter for assistive tech. */
export function Meter({
  value,
  label,
  valueText,
  tone = 'primary',
  marker,
  markerLabel,
}: {
  value: number
  label: string
  valueText?: string
  tone?: 'primary' | 'true' | 'false' | 'manip' | 'abstain' | 'neutral'
  marker?: number | null
  markerLabel?: string
}) {
  const v = Math.max(0, Math.min(1, value))
  const fill: Record<string, string> = {
    primary: 'bg-primary',
    true: 'bg-true-solid',
    false: 'bg-false-solid',
    manip: 'bg-manip-solid',
    abstain: 'bg-abstain-solid',
    neutral: 'bg-neutral-line',
  }
  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(v * 100)}
      aria-valuetext={valueText ?? `${Math.round(v * 100)}%`}
      className="relative h-3 w-full overflow-visible rounded-full bg-surface-2 ring-1 ring-border"
    >
      <div className={`h-full rounded-full ${fill[tone]}`} style={{ width: `${v * 100}%` }} />
      {marker != null ? (
        <div
          className="absolute -top-1 h-5 w-0.5 bg-fg"
          style={{ left: `calc(${Math.max(0, Math.min(1, marker)) * 100}% - 1px)` }}
          title={markerLabel}
          aria-hidden="true"
        />
      ) : null}
    </div>
  )
}

export function Spinner({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-muted" role="status">
      <Icon name="loader" size={16} />
      {label}
    </span>
  )
}

export function EmptyNote({ children }: { children: ReactNode }) {
  return <p className="rounded-lg border border-dashed border-border-strong p-3 text-sm text-muted">{children}</p>
}
