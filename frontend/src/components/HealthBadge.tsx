import type { ToolState, ToolStatusView } from '../api/types'
import { toolLabel } from '../lib/format'
import { isExtractive, isReplay, useHealth } from '../hooks/useHealth'
import { Icon } from './Icon'
import { Disclosure } from './ui'

const STATE_ORDER: ToolState[] = ['ok', 'degraded', 'open', 'chaos', 'unconfigured', 'disabled']
const STATE_TEXT: Record<ToolState, string> = {
  ok: 'ok',
  degraded: 'degraded',
  open: 'circuit open',
  unconfigured: 'unconfigured (no key)',
  disabled: 'disabled',
  chaos: 'fault injected',
}

export function toolSummary(tools: Record<string, ToolStatusView>): Array<[ToolState, number]> {
  const counts = new Map<ToolState, number>()
  for (const t of Object.values(tools)) counts.set(t.state, (counts.get(t.state) ?? 0) + 1)
  return STATE_ORDER.filter((s) => counts.has(s)).map((s) => [s, counts.get(s)!])
}

function stateCls(s: ToolState): string {
  if (s === 'ok') return 'text-true-fg'
  if (s === 'open' || s === 'chaos') return 'text-false-fg'
  if (s === 'degraded') return 'text-abstain-fg'
  return 'text-muted'
}

/** Footer badge from GET /health: resolved model ids, tool status summary, mode. */
export function HealthBadge() {
  const { health, error, loading, refresh } = useHealth()

  if (!health) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-sm text-muted" role="status">
        <Icon name={loading ? 'loader' : 'alert'} size={16} />
        {loading ? 'Checking backend status…' : `Backend status unavailable: ${error ?? 'unknown error'}`}
        {!loading ? (
          <button type="button" className="underline" onClick={refresh}>
            Retry
          </button>
        ) : null}
      </div>
    )
  }

  const models = Object.entries(health.resolved_models ?? {})
  const summary = toolSummary(health.tool_status ?? {})
  const replay = isReplay(health.mode)
  const extractive = isExtractive(health)

  return (
    <div className="space-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`inline-flex items-center gap-1 font-semibold ${health.status === 'ok' ? 'text-true-fg' : 'text-abstain-fg'}`}>
          <Icon name={health.status === 'ok' ? 'check-circle' : 'alert'} size={16} />
          Backend {health.status}
        </span>
        <span className="text-muted">v{health.version}</span>
        <span className={replay ? 'chip border-replay-bg bg-replay-bg text-replay-fg' : 'chip'}>
          {replay ? 'REPLAY MODE' : `mode: ${health.mode}`}
        </span>
        {extractive ? <span className="chip border-abstain-line bg-abstain-soft text-abstain-fg">LLM disabled (extractive mode)</span> : null}
      </div>
      <p className="break-words text-muted">
        <span className="font-semibold text-fg">LLM ({health.llm_provider}): </span>
        {models.length === 0
          ? 'no models resolved'
          : models.map(([role, id], i) => (
              <span key={role}>
                {i > 0 ? ' · ' : ''}
                {role} <code className="text-fg">{id}</code>
              </span>
            ))}
      </p>
      <Disclosure
        summary={
          <span>
            Tools:{' '}
            {summary.map(([s, n], i) => (
              <span key={s} className={stateCls(s)}>
                {i > 0 ? ' · ' : ''}
                {n} {STATE_TEXT[s]}
              </span>
            ))}
            <span className="text-muted"> · db {health.db}</span>
          </span>
        }
      >
        <ul className="grid grid-cols-1 gap-1 sm:grid-cols-2">
          {Object.entries(health.tool_status ?? {}).map(([name, t]) => (
            <li key={name} className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-2 border-b border-border py-1">
              <span className="min-w-0 break-words">
                {toolLabel(name)} {t.version ? <span className="text-muted">({t.version})</span> : null}
              </span>
              <span className={`text-xs font-semibold ${stateCls(t.state)}`}>
                {STATE_TEXT[t.state] ?? t.state}
                {t.failures ? ` · ${t.failures} failures` : ''}
              </span>
              {t.last_error ? <span className="w-full text-xs text-muted">last error: {t.last_error}</span> : null}
            </li>
          ))}
        </ul>
        {Object.keys(health.ml ?? {}).length ? (
          <p className="mt-2 text-xs text-muted">
            Local models:{' '}
            {Object.entries(health.ml)
              .map(([k, v]) => `${k} ${v}`)
              .join(' · ')}
          </p>
        ) : null}
      </Disclosure>
    </div>
  )
}
