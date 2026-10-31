import { describe, expect, it } from 'vitest'
import { detectPlatform, hostMatches, parseHttpUrl } from './platform'

describe('detectPlatform', () => {
  it.each([
    ['https://x.com/someone/status/1850000000000000000', 'x'],
    ['https://twitter.com/someone/status/1', 'x'],
    ['https://mobile.twitter.com/someone/status/1', 'x'],
    ['https://t.co/AbCdEf', 'x'],
    ['https://www.instagram.com/reel/C0ffee123/', 'instagram'],
    ['https://instagr.am/p/abc', 'instagram'],
    ['https://www.facebook.com/page/posts/123', 'facebook'],
    ['https://m.facebook.com/story.php?id=1', 'facebook'],
    ['https://fb.watch/abc123/', 'facebook'],
    ['https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'youtube'],
    ['https://youtu.be/dQw4w9WgXcQ', 'youtube'],
    ['https://m.youtube.com/shorts/abc', 'youtube'],
    ['https://www.tiktok.com/@user/video/123', 'tiktok'],
    ['https://vm.tiktok.com/ZMabc/', 'tiktok'],
    ['https://t.me/somechannel/123', 'telegram'],
    ['https://wa.me/919999999999', 'whatsapp'],
    ['https://chat.whatsapp.com/AbCdEf', 'whatsapp'],
    ['https://www.thehindu.com/news/national/article1.ece', 'web'],
    ['https://example.org/mock/x-post/1', 'web'],
    ['http://localhost:8000/thing', 'web'],
  ])('%s -> %s', (url, platform) => {
    expect(detectPlatform(url)).toBe(platform)
  })

  it('accepts links pasted without a scheme', () => {
    expect(detectPlatform('x.com/user/status/1')).toBe('x')
    expect(detectPlatform('  www.instagram.com/reel/abc  ')).toBe('instagram')
    expect(detectPlatform('youtu.be/abc')).toBe('youtube')
  })

  it('does not match look-alike hosts', () => {
    expect(detectPlatform('https://notx.com/a')).toBe('web')
    expect(detectPlatform('https://max.com/a')).toBe('web')
    expect(detectPlatform('https://x.com.evil.example/a')).toBe('web')
    expect(detectPlatform('https://myinstagram.com/a')).toBe('web')
    expect(detectPlatform('https://youtube.com.example.org/a')).toBe('web')
  })

  it('returns null for things that are not web links', () => {
    for (const s of ['', '   ', 'hello world', 'just text', 'javascript:alert(1)', 'ftp://x.com/a', 'mailto:a@b.com', 'x .com/a', null, undefined]) {
      expect(detectPlatform(s)).toBeNull()
    }
  })

  it('is case-insensitive on the host', () => {
    expect(detectPlatform('https://WWW.YouTube.COM/watch?v=1')).toBe('youtube')
  })
})

describe('parseHttpUrl / hostMatches', () => {
  it('normalizes a bare domain to https', () => {
    expect(parseHttpUrl('x.com/a')?.toString()).toBe('https://x.com/a')
  })
  it('matches exact host or subdomain only', () => {
    expect(hostMatches('www.x.com', 'x.com')).toBe(true)
    expect(hostMatches('x.com', 'x.com')).toBe(true)
    expect(hostMatches('box.com', 'x.com')).toBe(false)
  })
})
