import type { Platform } from '../api/types'
import { detectPlatform, parseHttpUrl } from './platform'

/**
 * Web Share Target (manifest `share_target`, GET /share?title=&text=&url=).
 * Android apps are inconsistent: many put the link inside `text` ("Watch this https://..."),
 * some in `url`, a few in `title`. We take the first http(s) link from url -> text -> title and keep
 * the remaining words as an optional claim text.
 */
export interface ShareInput {
  url: string | null
  platform: Platform | null
  /** shared words with the link removed (may be the claim itself, e.g. a forwarded message) */
  text: string | null
  title: string | null
}

const URL_IN_TEXT = /\bhttps?:\/\/[^\s<>"'`]+/i
const TRAILING_PUNCT = /[.,;:!?'"»”’\]>]+$/

/** First http(s) link inside free text, without trailing punctuation or unbalanced ")". */
export function extractFirstUrl(text: string | null | undefined): string | null {
  if (!text) return null
  const m = URL_IN_TEXT.exec(text)
  if (!m) return null
  let url = m[0].replace(TRAILING_PUNCT, '')
  // "(see https://x.com/a)" -> drop the closing paren unless the URL itself opened one.
  while (url.endsWith(')') && (url.match(/\(/g)?.length ?? 0) < (url.match(/\)/g)?.length ?? 0)) {
    url = url.slice(0, -1).replace(TRAILING_PUNCT, '')
  }
  return parseHttpUrl(url) ? url : null
}

function clean(s: string | null | undefined): string | null {
  const t = (s ?? '').replace(/\s+/g, ' ').trim()
  return t ? t : null
}

export function parseShareParams(params: URLSearchParams): ShareInput {
  const rawUrl = clean(params.get('url'))
  const rawText = clean(params.get('text'))
  const rawTitle = clean(params.get('title'))

  let url: string | null = null
  if (rawUrl) url = parseHttpUrl(rawUrl) ? rawUrl : extractFirstUrl(rawUrl)
  if (!url) url = extractFirstUrl(rawText)
  if (!url) url = extractFirstUrl(rawTitle)
  const normalized = url ? (parseHttpUrl(url)?.toString() ?? null) : null

  // Remaining words: text without the link (and without a duplicated title).
  let text = rawText
  if (text && url) text = clean(text.split(url).join(' '))
  if (text && rawTitle && text === rawTitle && normalized) text = null

  // No link and no text: a title alone is still a claim the user wanted checked.
  if (!normalized && !text && rawTitle) text = rawTitle

  return {
    url: normalized,
    platform: normalized ? detectPlatform(normalized) : null,
    text,
    title: rawTitle,
  }
}

/** Minimum length for leftover shared words to be worth checking together with the link. */
export const SHARED_TEXT_MIN_CHARS = 12
