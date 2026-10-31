import type { RetrievalMethod } from '../api/types'

export function formatMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`
  const m = Math.floor(ms / 60_000)
  const s = Math.round((ms % 60_000) / 1000)
  return `${m} min ${s} s`
}

export function pct(x: number | null | undefined, digits = 0): string {
  if (x == null || !Number.isFinite(x)) return '—'
  return `${(x * 100).toFixed(digits)}%`
}

export function num(x: number | null | undefined, digits = 2): string {
  if (x == null || !Number.isFinite(x)) return '—'
  return x.toFixed(digits)
}

export function signed(x: number | null | undefined, digits = 2): string {
  if (x == null || !Number.isFinite(x)) return '—'
  return `${x > 0 ? '+' : ''}${x.toFixed(digits)}`
}

const dateTimeFmt = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' })
const dateFmt = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium' })

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : dateTimeFmt.format(d)
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : dateFmt.format(d)
}

/** "a1b2c3d4e5…9f8e7d" for long hashes; full value stays available via title/copy. */
export function shortHash(h: string | null | undefined, head = 10, tail = 6): string {
  if (!h) return '—'
  return h.length <= head + tail + 1 ? h : `${h.slice(0, head)}…${h.slice(-tail)}`
}

export function humanize(s: string): string {
  return s.replace(/[_-]+/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
}

export const RETRIEVAL_LABELS: Record<RetrievalMethod, string> = {
  factcheck_claimsearch: 'Google Fact Check (claim search)',
  factcheck_imagesearch: 'Google Fact Check (image search)',
  google_news_rss: 'Google News RSS',
  wire_rss: 'Wire RSS',
  pib_rss: 'PIB RSS',
  gdelt_doc: 'GDELT DOC 2.0',
  serper_web: 'Serper web search',
  serper_news: 'Serper news search',
  wikidata_sparql: 'Wikidata SPARQL',
  vision_web_detection: 'Cloud Vision web detection',
  phash_index: 'pHash index',
  clip_index: 'CLIP index',
  followup_search: 'Debate follow-up search',
}

export function retrievalLabel(m: string): string {
  return (RETRIEVAL_LABELS as Record<string, string | undefined>)[m] ?? humanize(m)
}

/** Registered tool names (core/tools_registry.py) -> display names. */
export const TOOL_LABELS: Record<string, string> = {
  factcheck: 'Google Fact Check',
  gnews_rss: 'Google News RSS',
  wire_rss: 'Wire RSS',
  pib_rss: 'PIB RSS',
  gdelt: 'GDELT',
  serper: 'Serper search',
  wikidata: 'Wikidata',
  vision_web: 'Cloud Vision web detection',
  sightengine: 'Sightengine deepfake API',
  hf_inference: 'HF Inference',
  llm_primary: 'Primary LLM',
  llm_secondary: 'Second-judge LLM',
  oembed: 'oEmbed',
  page_fetch: 'Page fetch',
  ytdlp: 'yt-dlp',
  playwright_fetch: 'Browser fetch (Playwright)',
}

export function toolLabel(tool: string): string {
  if (tool.startsWith('node:')) return `Pipeline step "${tool.slice(5)}"`
  return TOOL_LABELS[tool] ?? tool
}

export function degradedToolLabel(tool: string): string {
  return `${toolLabel(tool)} unavailable — result degraded`
}

/** "processing (rate-limited: serper)" -> "serper" */
export function rateLimitedTool(detail: string | null | undefined): string | null {
  if (!detail) return null
  const m = /rate[- ]?limit(?:ed)?\s*[:(]?\s*([\w.-]+)/i.exec(detail)
  return m?.[1] ?? null
}
