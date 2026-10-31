import { describe, expect, it } from 'vitest'
import { splitPlaceholders } from './placeholders'

describe('splitPlaceholders', () => {
  it('highlights server-listed and generic placeholders, keeps all text', () => {
    const text = 'To: [GRIEVANCE OFFICER NAME]\nI, [YOUR FULL NAME], {{date}} <PLATFORM NAME>. Section 3(2)(b) [x].'
    const parts = splitPlaceholders(text, ['[YOUR FULL NAME]'])
    expect(parts.map((p) => p.text).join('')).toBe(text)
    expect(parts.filter((p) => p.placeholder).map((p) => p.text)).toEqual(['[GRIEVANCE OFFICER NAME]', '[YOUR FULL NAME]', '{{date}}', '<PLATFORM NAME>'])
  })

  it('escapes regex characters in listed placeholders', () => {
    const parts = splitPlaceholders('Sign: (signature here)?', ['(signature here)?'])
    expect(parts).toEqual([
      { text: 'Sign: ', placeholder: false },
      { text: '(signature here)?', placeholder: true },
    ])
  })

  it('returns one plain part when there are no placeholders', () => {
    expect(splitPlaceholders('plain text')).toEqual([{ text: 'plain text', placeholder: false }])
  })
})
