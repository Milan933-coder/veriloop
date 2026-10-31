/**
 * Mock mode serves typed fixtures from src/mocks instead of the backend.
 * - `VITE_MOCK=1` (npm run dev:mock / build:mock) turns it on for the whole build.
 * - `?mock=1` turns it on for this tab, in dev builds only (`?mock=0` turns it off).
 * Production builds without VITE_MOCK cannot enter mock mode, and the fixtures are tree-shaken out.
 */
const KEY = 'veriloop.mock'

export const MOCK_ALLOWED: boolean = __MOCK_ALLOWED__

function detect(): boolean {
  if (!MOCK_ALLOWED) return false
  if (import.meta.env.VITE_MOCK === '1') return true
  if (typeof window === 'undefined') return false
  try {
    const q = new URLSearchParams(window.location.search).get('mock')
    if (q === '1') window.sessionStorage.setItem(KEY, '1')
    else if (q === '0') window.sessionStorage.removeItem(KEY)
    return window.sessionStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

export const MOCK: boolean = detect()

/** Dev-only knobs for mock mode, read from the current URL (e.g. ?sse=fail, ?health=replay, ?tamper=1). */
export function mockFlag(name: string): string | null {
  if (!MOCK || typeof window === 'undefined') return null
  try {
    return new URLSearchParams(window.location.search).get(name)
  } catch {
    return null
  }
}
