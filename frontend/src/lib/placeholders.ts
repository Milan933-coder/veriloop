export interface TextPart {
  text: string
  placeholder: boolean
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** Generic placeholder shapes used by templates: [YOUR NAME], {{name}}, <NAME OF PLATFORM>. */
const GENERIC = String.raw`\[[A-Z0-9][A-Z0-9 _\-/'’.,()]*\]|\{\{[^{}\n]+\}\}|<[A-Z][A-Z0-9 _\-]+>`

/**
 * Split a legal draft into plain text and placeholder parts so the UI can highlight what the user
 * must fill in. Uses the server's `placeholders` list plus generic bracket patterns.
 */
export function splitPlaceholders(text: string, placeholders: readonly string[] = []): TextPart[] {
  const explicit = [...placeholders].filter(Boolean).sort((a, b) => b.length - a.length).map(escapeRe)
  const re = new RegExp([...explicit, GENERIC].join('|'), 'g')
  const parts: TextPart[] = []
  let last = 0
  for (const m of text.matchAll(re)) {
    const at = m.index ?? 0
    if (m[0].length === 0) continue
    if (at > last) parts.push({ text: text.slice(last, at), placeholder: false })
    parts.push({ text: m[0], placeholder: true })
    last = at + m[0].length
  }
  if (last < text.length) parts.push({ text: text.slice(last), placeholder: false })
  return parts
}
