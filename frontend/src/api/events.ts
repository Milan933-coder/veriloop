import { parseEventData } from '../lib/trace'
import { MOCK, MOCK_ALLOWED } from '../lib/mockMode'
import { links, loadMock } from './client'
import type { JobEvent } from './types'

export interface JobEventHandlers {
  onEvent: (ev: JobEvent) => void
  onOpen?: () => void
  /** The stream failed or closed. The caller falls back to polling GET /verify/{id}. */
  onClosed: () => void
}

/**
 * Event names we listen for besides the default (unnamed) `message` event, in case the backend uses
 * sse-starlette's `event:` field. The payload `type` wins over the event name when present.
 */
const NAMED_EVENTS = ['node', 'status', 'job', 'progress', 'tool', 'log', 'done', 'complete', 'completed', 'verdict', 'end']

/** Subscribe to `GET /verify/{id}/events` (SSE). Returns an unsubscribe function. */
export function subscribeJobEvents(jobId: string, h: JobEventHandlers): () => void {
  if (MOCK_ALLOWED && MOCK) {
    let unsub: (() => void) | null = null
    let cancelled = false
    void loadMock().then((m) => {
      if (!cancelled) unsub = m.mockSubscribe(jobId, h)
    })
    return () => {
      cancelled = true
      unsub?.()
    }
  }

  if (typeof EventSource === 'undefined') {
    queueMicrotask(h.onClosed)
    return () => {}
  }

  const es = new EventSource(links.events(jobId))
  let closed = false
  const close = () => {
    if (closed) return
    closed = true
    es.close()
  }
  const handle = (name?: string) => (e: MessageEvent) => {
    const ev = parseEventData(typeof e.data === 'string' ? e.data : '', name)
    if (ev) h.onEvent(ev)
  }
  es.onopen = () => h.onOpen?.()
  es.onmessage = handle()
  for (const name of NAMED_EVENTS) es.addEventListener(name, handle(name) as EventListener)
  // A server-sent `event: error` arrives as a MessageEvent with data; a connection failure has no data.
  es.addEventListener('error', (e) => {
    if (e instanceof MessageEvent && typeof e.data === 'string' && e.data) {
      handle('error')(e)
      return
    }
    // EventSource would auto-reconnect forever; we prefer a clean switch to polling.
    close()
    h.onClosed()
  })
  return close
}
