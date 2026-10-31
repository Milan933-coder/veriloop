import { useEffect, useMemo, useState } from 'react'
import type { JobView } from '../api/types'
import type { Connection } from '../hooks/useJob'
import { formatMs } from '../lib/format'
import {
  NODE_INFO,
  liveToTimings,
  mergeTimings,
  modelFromLive,
  modelFromTimings,
  nodeLabel,
  tickStep,
  type LiveTrace,
  type RowStatus,
  type TraceModel,
  type TraceRow,
} from '../lib/trace'
import { Icon, type IconName } from './Icon'
import { Disclosure, Section } from './ui'

const STATUS: Record<RowStatus, { icon: IconName; text: string; cls: string }> = {
  pending: { icon: 'clock', text: 'waiting', cls: 'text-muted' },
  running: { icon: 'loader', text: 'running', cls: 'text-primary' },
  ok: { icon: 'check-circle', text: 'done', cls: 'text-true-fg' },
  error: { icon: 'alert', text: 'error', cls: 'text-false-fg' },
  skipped: { icon: 'ban', text: 'skipped', cls: 'text-muted' },
}

const CONNECTION: Record<Connection, { text: string; icon: IconName }> = {
  connecting: { text: 'Connecting…', icon: 'loader' },
  sse: { text: 'Live (server-sent events)', icon: 'activity' },
  polling: { text: 'Live stream unavailable: polling every 1.5 s', icon: 'clock' },
  done: { text: 'Run finished', icon: 'check-circle' },
  error: { text: 'Not available', icon: 'alert' },
}

/** Re-render ~10x/s while something is running so bars grow smoothly. */
function useTicker(active: boolean): number {
  const [now, setNow] = useState(() => performance.now())
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => setNow(performance.now()), 100)
    return () => clearInterval(t)
  }, [active])
  return now
}

function Bar({ row, total }: { row: TraceRow; total: number }) {
  if (row.start == null) return null
  const left = (row.start / total) * 100
  const width = Math.max((((row.end ?? row.start) - row.start) / total) * 100, 0.8)
  const color =
    row.status === 'error'
      ? 'bg-false-solid'
      : row.status === 'skipped'
        ? 'border border-dashed border-border-strong bg-transparent'
        : row.status === 'running'
          ? 'bg-primary bar-running'
          : 'bg-primary'
  const ms = row.durationMs ?? (row.end != null ? row.end - row.start : null)
  return (
    <div
      className={`absolute top-1/2 h-2.5 -translate-y-1/2 rounded ${color}`}
      style={{ left: `${Math.min(left, 99.2)}%`, width: `${Math.min(width, 100 - Math.min(left, 99.2))}%` }}
      title={`${nodeLabel(row.node)}: ${STATUS[row.status].text}${ms != null ? `, ${formatMs(ms)}` : ''} (starts at ${formatMs(row.start)})`}
    />
  )
}

function Row({ row, total }: { row: TraceRow; total: number }) {
  const st = STATUS[row.status]
  const info = NODE_INFO[row.node]
  const ms = row.status === 'running' ? null : row.durationMs
  const timeText = row.status === 'running' && row.start != null && row.end != null ? `${formatMs(row.end - row.start)}…` : ms != null ? formatMs(ms) : row.status === 'skipped' ? 'not run' : ''
  return (
    <li className="grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)] items-center gap-x-3 py-1 sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)_4.75rem]">
      <div className="min-w-0">
        <p className="flex items-center gap-1.5 text-sm font-semibold" title={info?.hint}>
          <Icon name={st.icon} size={15} className={st.cls} />
          <span className="truncate">{nodeLabel(row.node)}</span>
        </p>
        <p className="truncate text-xs text-muted sm:hidden">
          {timeText || st.text}
        </p>
        <span className="sr-only">
          {`${st.text}${timeText ? `, ${timeText}` : ''}${row.detail ? `, ${row.detail}` : ''}`}
        </span>
      </div>
      <div className="relative h-6" aria-hidden="true">
        <Bar row={row} total={total} />
      </div>
      <p className="hidden text-right text-xs tabular-nums text-muted sm:block" aria-hidden="true">
        {timeText || st.text}
      </p>
      {row.detail && row.status !== 'pending' && row.detail !== timeText ? (
        <p className="col-span-full -mt-1 truncate pl-6 text-xs text-muted" aria-hidden="true">
          {row.detail}
        </p>
      ) : null}
    </li>
  )
}

/** Compact tick labels: 0, 500 ms, 2 s, 2.5 s, 10 s. */
function axisLabel(ms: number): string {
  if (ms === 0) return '0'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${Number((ms / 1000).toFixed(1))} s`
}

function Axis({ total }: { total: number }) {
  const step = tickStep(total, 5)
  const ticks: number[] = []
  for (let t = 0; t <= total + 1e-6; t += step) ticks.push(t)
  return (
    <div className="grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)] gap-x-3 sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)_4.75rem]" aria-hidden="true">
      <span className="text-xs text-muted">Step</span>
      <div className="relative h-5 border-b border-border">
        {ticks.map((t) => (
          <span
            key={t}
            className="absolute bottom-0.5 -translate-x-1/2 text-[11px] tabular-nums text-muted first:translate-x-0"
            style={{ left: `${(t / total) * 100}%` }}
          >
            {axisLabel(t)}
          </span>
        ))}
      </div>
      <span className="hidden text-right text-xs text-muted sm:block">Time</span>
    </div>
  )
}

export function TraceTimeline({ job, live, connection }: { job: JobView | null; live: LiveTrace; connection: Connection }) {
  const terminal = job?.status === 'completed' || job?.status === 'failed'
  const hasLive = live.order.length > 0
  const ticking = !terminal && connection !== 'done' && (hasLive || !!job?.trace?.length)
  const now = useTicker(ticking)

  const model: TraceModel = useMemo(() => {
    const server = job?.trace ?? []
    // Finished: the server NodeTiming list is authoritative.
    if (terminal && server.length) return modelFromTimings(server, true)
    // Running with a partial server trace: merge it with live SSE timings (a reconnect replays past
    // events in a burst, so their receive times are meaningless; server timestamps are not).
    if (server.length) {
      const perfToWall = Date.now() - performance.now()
      return modelFromTimings(mergeTimings(server, liveToTimings(live, perfToWall)), terminal, now + perfToWall)
    }
    return modelFromLive(live, now, terminal)
  }, [terminal, job?.trace, live, now])

  const total = Math.max(model.totalMs, 1000) * 1.04
  const firstParallel = model.rows.findIndex((r) => r.parallel)
  const head = firstParallel < 0 ? [] : model.rows.slice(0, firstParallel)
  const parallel = model.rows.filter((r) => r.parallel)
  const post = firstParallel < 0 ? model.rows : model.rows.slice(firstParallel).filter((r) => !r.parallel)
  const c = CONNECTION[connection]
  const finishedMs = terminal ? model.totalMs : null

  return (
    <Section
      id="trace"
      title="Orchestrator trace"
      icon="activity"
      subtitle="Each pipeline step with its timing. Evidence, forensics and provenance run in parallel."
      actions={
        <span className="chip" role="status">
          <Icon name={c.icon} size={14} />
          {c.text}
        </span>
      }
    >
      <Axis total={total} />
      <ol aria-label="Pipeline steps (before fan-out)" className="divide-y divide-border/60">
        {head.map((r) => (
          <Row key={r.node} row={r} total={total} />
        ))}
      </ol>
      {parallel.length ? (
        <div className="my-1 rounded-lg border border-primary/40 bg-primary-soft/60 px-2 py-1">
          <p className="flex items-center gap-1.5 pt-1 text-xs font-bold uppercase tracking-wide text-muted">
            <Icon name="layers" size={14} /> Parallel fan-out (async)
          </p>
          <ol aria-label="Parallel branches" className="divide-y divide-border/60">
            {parallel.map((r) => (
              <Row key={r.node} row={r} total={total} />
            ))}
          </ol>
        </div>
      ) : null}
      <ol aria-label="Pipeline steps (after fan-out)" className="divide-y divide-border/60">
        {post.map((r) => (
          <Row key={r.node} row={r} total={total} />
        ))}
      </ol>
      <p className="mt-2 text-sm text-muted">
        {finishedMs != null ? (
          <>
            Total run time <strong className="text-fg tabular-nums">{formatMs(finishedMs)}</strong>
          </>
        ) : (
          'Timings update live as each step starts and finishes.'
        )}
      </p>
      {live.others.length ? (
        <Disclosure className="mt-3" summary={`Event log (${live.others.length} other event${live.others.length === 1 ? '' : 's'})`}>
          <ol className="space-y-1 font-mono text-xs">
            {live.others.map((o, i) => (
              <li key={i} className="break-all">
                <span className="text-muted">+{formatMs(o.at)}</span> <strong>{o.event.type}</strong>{' '}
                {JSON.stringify(o.event, (k, v) => (k === 'type' ? undefined : v)).slice(0, 200)}
              </li>
            ))}
          </ol>
        </Disclosure>
      ) : null}
    </Section>
  )
}
