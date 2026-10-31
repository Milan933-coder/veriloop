import type { Platform } from '../api/types'

/** Registrable host suffixes per platform. A host matches when it equals a suffix or ends with ".<suffix>". */
const PLATFORM_HOSTS: ReadonlyArray<readonly [Platform, readonly string[]]> = [
  ['x', ['x.com', 'twitter.com', 't.co']],
  ['instagram', ['instagram.com', 'instagr.am']],
  ['facebook', ['facebook.com', 'fb.com', 'fb.watch', 'fb.me']],
  ['youtube', ['youtube.com', 'youtu.be', 'youtube-nocookie.com']],
  ['tiktok', ['tiktok.com']],
  ['telegram', ['t.me', 'telegram.me', 'telegram.dog']],
  ['whatsapp', ['whatsapp.com', 'wa.me']],
]

export const PLATFORM_LABELS: Record<Platform, string> = {
  x: 'X (Twitter)',
  instagram: 'Instagram',
  facebook: 'Facebook',
  youtube: 'YouTube',
  tiktok: 'TikTok',
  telegram: 'Telegram',
  whatsapp: 'WhatsApp',
  web: 'Other website',
  upload: 'Uploaded file',
  text: 'Text claim',
}

/** Platforms a user can pick for a link (upload/text are set by the input mode). */
export const LINK_PLATFORMS: Platform[] = ['x', 'instagram', 'facebook', 'youtube', 'tiktok', 'telegram', 'whatsapp', 'web']

const BARE_DOMAIN = /^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]|$)/i

/**
 * Parses user input into an http(s) URL. Accepts a missing scheme ("x.com/user/status/1").
 * Returns null for anything that is not a plausible web link.
 */
export function parseHttpUrl(input: string | null | undefined): URL | null {
  const raw = (input ?? '').trim()
  if (!raw || /\s/.test(raw)) return null
  let candidate = raw
  if (!/^[a-z][a-z0-9+.-]*:/i.test(candidate)) {
    if (candidate.startsWith('//')) candidate = `https:${candidate}`
    else if (BARE_DOMAIN.test(candidate)) candidate = `https://${candidate}`
    else return null
  }
  let url: URL
  try {
    url = new URL(candidate)
  } catch {
    return null
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
  if (!url.hostname.includes('.') && url.hostname !== 'localhost') return null
  return url
}

export function hostMatches(host: string, suffix: string): boolean {
  const h = host.toLowerCase().replace(/\.$/, '')
  return h === suffix || h.endsWith(`.${suffix}`)
}

/** URL -> platform. Unknown hosts are "web"; invalid input is null. */
export function detectPlatform(input: string | null | undefined): Platform | null {
  const url = parseHttpUrl(input)
  if (!url) return null
  for (const [platform, hosts] of PLATFORM_HOSTS) {
    if (hosts.some((suffix) => hostMatches(url.hostname, suffix))) return platform
  }
  return 'web'
}
