import { describe, expect, it } from 'vitest'
import { extractFirstUrl, parseShareParams } from './share'

const q = (o: Record<string, string>) => new URLSearchParams(o)

describe('parseShareParams (PWA share_target GET /share?title=&text=&url=)', () => {
  it('uses the url param when present', () => {
    const r = parseShareParams(q({ url: 'https://www.instagram.com/reel/C0ffee/', title: 'Instagram', text: '' }))
    expect(r.url).toBe('https://www.instagram.com/reel/C0ffee/')
    expect(r.platform).toBe('instagram')
    expect(r.text).toBeNull()
  })

  it('extracts the link from text (how most Android apps share)', () => {
    const r = parseShareParams(q({ text: 'Watch this before they delete it! https://youtu.be/abc123?si=xyz' }))
    expect(r.url).toBe('https://youtu.be/abc123?si=xyz')
    expect(r.platform).toBe('youtube')
    expect(r.text).toBe('Watch this before they delete it!')
  })

  it('extracts the link from the title as a last resort', () => {
    const r = parseShareParams(q({ title: 'Post https://x.com/user/status/123' }))
    expect(r.url).toBe('https://x.com/user/status/123')
    expect(r.platform).toBe('x')
  })

  it('strips trailing punctuation and unbalanced parentheses', () => {
    expect(extractFirstUrl('see (https://x.com/a/status/1).')).toBe('https://x.com/a/status/1')
    expect(extractFirstUrl('link: https://t.me/chan/5, ok?')).toBe('https://t.me/chan/5')
    expect(extractFirstUrl('https://en.wikipedia.org/wiki/Foo_(bar)')).toBe('https://en.wikipedia.org/wiki/Foo_(bar)')
    expect(extractFirstUrl('“https://wa.me/123”')).toBe('https://wa.me/123')
  })

  it('treats shared text without a link as the claim', () => {
    const r = parseShareParams(q({ text: 'Govt giving free laptops to all students, forward to 10 groups' }))
    expect(r.url).toBeNull()
    expect(r.platform).toBeNull()
    expect(r.text).toBe('Govt giving free laptops to all students, forward to 10 groups')
  })

  it('falls back to the title when there is no text or link', () => {
    const r = parseShareParams(q({ title: 'Breaking: dam has burst near the city' }))
    expect(r.url).toBeNull()
    expect(r.text).toBe('Breaking: dam has burst near the city')
  })

  it('collapses whitespace and handles a link that appears in both url and text', () => {
    const r = parseShareParams(q({ url: 'https://x.com/a/status/1', text: '  Look   at this\n https://x.com/a/status/1 ' }))
    expect(r.url).toBe('https://x.com/a/status/1')
    expect(r.text).toBe('Look at this')
  })

  it('rejects non-http links and returns empty for nothing shared', () => {
    expect(parseShareParams(q({ url: 'javascript:alert(1)' })).url).toBeNull()
    const empty = parseShareParams(q({}))
    expect(empty).toEqual({ url: null, platform: null, text: null, title: null })
  })

  it('accepts a url param without scheme', () => {
    const r = parseShareParams(q({ url: 'www.facebook.com/page/posts/1' }))
    expect(r.url).toBe('https://www.facebook.com/page/posts/1')
    expect(r.platform).toBe('facebook')
  })
})
