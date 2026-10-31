/** Locales offered for claim language / evidence search (BCP 47). en-IN is the default. */
export const LOCALES: Array<{ id: string; label: string }> = [
  { id: 'en-IN', label: 'English (India)' },
  { id: 'hi-IN', label: 'हिन्दी (Hindi)' },
  { id: 'bn-IN', label: 'বাংলা (Bengali)' },
  { id: 'ta-IN', label: 'தமிழ் (Tamil)' },
  { id: 'te-IN', label: 'తెలుగు (Telugu)' },
  { id: 'mr-IN', label: 'मराठी (Marathi)' },
  { id: 'gu-IN', label: 'ગુજરાતી (Gujarati)' },
  { id: 'kn-IN', label: 'ಕನ್ನಡ (Kannada)' },
  { id: 'ml-IN', label: 'മലയാളം (Malayalam)' },
  { id: 'pa-IN', label: 'ਪੰਜਾਬੀ (Punjabi)' },
  { id: 'ur-IN', label: 'اردو (Urdu)' },
  { id: 'en-US', label: 'English (US)' },
  { id: 'en-GB', label: 'English (UK)' },
]

export const DEFAULT_LOCALE = 'en-IN'

/** Best match for the browser language among LOCALES, else en-IN. */
export function preferredLocale(langs: readonly string[] | undefined): string {
  for (const l of langs ?? []) {
    const exact = LOCALES.find((x) => x.id.toLowerCase() === l.toLowerCase())
    if (exact) return exact.id
    const base = l.split('-')[0]?.toLowerCase()
    const inIndia = LOCALES.find((x) => x.id.toLowerCase() === `${base}-in`)
    if (base && base !== 'en' && inIndia) return inIndia.id
  }
  return DEFAULT_LOCALE
}
