import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { ApiError, errorMessage } from '../api/errors'
import { subscribeJobEvents } from '../api/events'
import { JOB_STATUSES, type JobEvent, type JobStatus, type JobView } from '../api/types'
import { applyEvent, emptyLiveTrace, isNodeEvent, isTerminalEvent, type LiveTrace } from '../lib/trace'

export type Connection = 'connecting' | 'sse' | 'polling' | 'done' | 'error'

export const POLL_FALLBACK_MS = 1500
/** While SSE is healthy we still refresh the job view slowly (status_detail, queue position). */
const POLL_WITH_SSE_MS = 4000

export function isTerminalStatus(s: JobStatus | null | undefined): boolean {
  return s === 'completed' || s === 'failed'
}

export interface JobHook {
  job: JobView | null
  error: string | null
  notFound: boolean
  connection: Connection
  live: LiveTrace
  refresh: () => void
}

function asStatus(v: unknown): JobStatus | null {
  return typeof v === 'string' && (JOB_STATUSES as readonly string[]).includes(v) ? (v as JobStatus) : null
}

/** Merge a non-node SSE event into the job view (status / status_detail / queue_position). */
function mergeStatus(job: JobView | null, ev: JobEvent): JobView | null {
  if (!job || isTerminalStatus(job.status) || isNodeEvent(ev)) return job
  const e = ev as Record<string, unknown>
  let next = job
  const st = asStatus(e.status)
  // A status change without a detail clears the stale one (e.g. "queued (position 1)").
  if (st && !isTerminalStatus(st)) next = { ...next, status: st, status_detail: st !== job.status ? null : next.status_detail }
  if ('status_detail' in e) next = { ...next, status_detail: typeof e.status_detail === 'string' ? e.status_detail : null }
  if (typeof e.queue_position === 'number') next = { ...next, queue_position: e.queue_position }
  if (e.status === 'rate_limited' && typeof e.tool === 'string') next = { ...next, status_detail: `processing (rate-limited: ${e.tool})` }
  return next
}

/**
 * Live job state: GET /verify/{id} first; if not finished, subscribe to SSE /verify/{id}/events,
 * and fall back to polling GET /verify/{id} every 1.5 s if the stream fails.
 */
export function useJob(jobId: string): JobHook {
  const [job, setJob] = useState<JobView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)
  const [connection, setConnection] = useState<Connection>('connecting')
  const [live, setLive] = useState<LiveTrace>(emptyLiveTrace)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let alive = true
    let terminal = false
    let mode: 'sse' | 'polling' = 'sse'
    let unsub: (() => void) | null = null
    let pollTimer: ReturnType<typeof setTimeout> | null = null

    setJob(null)
    setError(null)
    setNotFound(false)
    setConnection('connecting')
    setLive(emptyLiveTrace())

    const stopPoll = () => {
      if (pollTimer) clearTimeout(pollTimer)
      pollTimer = null
    }
    const finish = (c: Connection) => {
      terminal = true
      unsub?.()
      unsub = null
      stopPoll()
      setConnection(c)
    }
    const fetchJob = async (): Promise<JobView | null> => {
      try {
        const v = await api.job(jobId)
        if (!alive) return null
        setJob(v)
        setError(null)
        if (isTerminalStatus(v.status)) finish('done')
        return v
      } catch (e) {
        if (!alive) return null
        if (e instanceof ApiError && e.status === 404) {
          setNotFound(true)
          finish('error')
        }
        setError(errorMessage(e))
        return null
      }
    }
    const schedulePoll = (ms: number) => {
      stopPoll()
      if (terminal) return
      pollTimer = setTimeout(async () => {
        if (!alive || terminal) return
        await fetchJob()
        schedulePoll(mode === 'sse' ? POLL_WITH_SSE_MS : POLL_FALLBACK_MS)
      }, ms)
    }

    void (async () => {
      await fetchJob()
      if (!alive || terminal) return
      unsub = subscribeJobEvents(jobId, {
        onOpen: () => alive && !terminal && setConnection('sse'),
        onEvent: (ev) => {
          if (!alive) return
          setLive((s) => applyEvent(s, ev, performance.now()))
          if (!isNodeEvent(ev)) setJob((j) => mergeStatus(j, ev))
          if (isTerminalEvent(ev) && !terminal) {
            // The run is (about to be) finished: fetch now, then poll quickly until the view says so.
            void fetchJob().then(() => schedulePoll(600))
          }
        },
        onClosed: () => {
          if (!alive || terminal) return
          unsub = null
          mode = 'polling'
          setConnection('polling')
          schedulePoll(0)
        },
      })
      schedulePoll(POLL_WITH_SSE_MS)
    })()

    return () => {
      alive = false
      unsub?.()
      stopPoll()
    }
  }, [jobId, nonce])

  const refresh = useCallback(() => setNonce((n) => n + 1), [])
  return { job, error, notFound, connection, live, refresh }
}
