// Generates the VeriLoop PWA icons (PNG + SVG) with zero dependencies.
// Mark: a "loop" arrow (closed-loop enforcement) around a check mark (verified).
// Run: npm run icons   (outputs into public/ and public/icons/)
import { deflateSync } from 'node:zlib'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', 'public')
const BG = [0x0f, 0x3d, 0x5e] // deep civic blue
const FG = [0xff, 0xff, 0xff]
const DEG = Math.PI / 180

// Geometry in a 512x512 design space.
const C = 256
const R = 150
const RING_W = 34
const GAP_FROM = -70 * DEG // arc runs clockwise from GAP_TO to GAP_FROM (+360)
const GAP_TO = -25 * DEG
const CHECK = [
  [180, 262],
  [234, 316],
  [340, 206],
]
const CHECK_W = 40

function arrowHead() {
  const a = GAP_FROM
  const end = [C + R * Math.cos(a), C + R * Math.sin(a)]
  const dir = [-Math.sin(a), Math.cos(a)] // clockwise tangent
  const nrm = [Math.cos(a), Math.sin(a)]
  const tip = [end[0] + dir[0] * 46, end[1] + dir[1] * 46]
  const base = [end[0] - dir[0] * 8, end[1] - dir[1] * 8]
  return [tip, [base[0] + nrm[0] * 40, base[1] + nrm[1] * 40], [base[0] - nrm[0] * 40, base[1] - nrm[1] * 40]]
}
const TRI = arrowHead()

function segDist(p, a, b) {
  const vx = b[0] - a[0]
  const vy = b[1] - a[1]
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / (vx * vx + vy * vy)))
  return Math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy)
}

function inTri(p, [a, b, c]) {
  const s = (p1, p2, p3) => (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
  const d1 = s(p, a, b)
  const d2 = s(p, b, c)
  const d3 = s(p, c, a)
  const neg = d1 < 0 || d2 < 0 || d3 < 0
  const pos = d1 > 0 || d2 > 0 || d3 > 0
  return !(neg && pos)
}

function inRing(p) {
  const d = Math.hypot(p[0] - C, p[1] - C)
  if (Math.abs(d - R) > RING_W / 2) {
    // round cap at the arc start (the arrow head covers the other end)
    const s = [C + R * Math.cos(GAP_TO), C + R * Math.sin(GAP_TO)]
    return Math.hypot(p[0] - s[0], p[1] - s[1]) <= RING_W / 2
  }
  const ang = Math.atan2(p[1] - C, p[0] - C)
  return !(ang > GAP_FROM && ang < GAP_TO)
}

function inCheck(p) {
  return segDist(p, CHECK[0], CHECK[1]) <= CHECK_W / 2 || segDist(p, CHECK[1], CHECK[2]) <= CHECK_W / 2
}

function inRoundRect(p, radius) {
  const qx = Math.abs(p[0] - C) - (C - radius)
  const qy = Math.abs(p[1] - C) - (C - radius)
  const outside = Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - radius
  return outside <= 0
}

/** @param {number} size @param {{radius:number, scale:number}} opt */
function render(size, opt) {
  const SS = 4
  const px = Buffer.alloc(size * size * 4)
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let bg = 0
      let fg = 0
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const dx = ((x + (sx + 0.5) / SS) / size) * 512
          const dy = ((y + (sy + 0.5) / SS) / size) * 512
          if (!inRoundRect([dx, dy], opt.radius)) continue
          bg++
          // foreground is scaled about the centre (maskable icons keep a safe zone)
          const fx = C + (dx - C) / opt.scale
          const fy = C + (dy - C) / opt.scale
          const fp = [fx, fy]
          if (inRing(fp) || inTri(fp, TRI) || inCheck(fp)) fg++
        }
      }
      const n = SS * SS
      const i = (y * size + x) * 4
      const a = bg / n
      const f = bg ? fg / bg : 0
      for (let k = 0; k < 3; k++) px[i + k] = Math.round(BG[k] * (1 - f) + FG[k] * f)
      px[i + 3] = Math.round(a * 255)
    }
  }
  return encodePng(size, size, px)
}

const CRC_TABLE = new Uint32Array(256).map((_, n) => {
  let c = n
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
  return c >>> 0
})
function crc32(buf) {
  let c = 0xffffffff
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}
function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const td = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(td))
  return Buffer.concat([len, td, crc])
}
function encodePng(w, h, rgba) {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(w, 0)
  ihdr.writeUInt32BE(h, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 6 // RGBA
  const raw = Buffer.alloc((w * 4 + 1) * h)
  for (let y = 0; y < h; y++) {
    raw[y * (w * 4 + 1)] = 0
    rgba.copy(raw, y * (w * 4 + 1) + 1, y * w * 4, (y + 1) * w * 4)
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

function svg({ radius, scale }) {
  const f = (n) => Math.round(n * 10) / 10
  const pt = (a) => `${f(C + R * Math.cos(a))} ${f(C + R * Math.sin(a))}`
  const tri = TRI.map((p) => `${f(p[0])},${f(p[1])}`).join(' ')
  const hex = `#${BG.map((c) => c.toString(16).padStart(2, '0')).join('')}`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="VeriLoop">
  <rect width="512" height="512" rx="${radius}" fill="${hex}"/>
  <g transform="translate(256 256) scale(${scale}) translate(-256 -256)" fill="none" stroke="#fff" stroke-linecap="round" stroke-linejoin="round">
    <path d="M ${pt(GAP_TO)} A ${R} ${R} 0 1 1 ${pt(GAP_FROM)}" stroke-width="${RING_W}" stroke-linecap="butt"/>
    <polygon points="${tri}" fill="#fff" stroke="none"/>
    <polyline points="${CHECK.map((p) => p.join(',')).join(' ')}" stroke-width="${CHECK_W}"/>
  </g>
</svg>
`
}

function write(rel, data) {
  const p = join(ROOT, rel)
  mkdirSync(dirname(p), { recursive: true })
  writeFileSync(p, data)
  console.log('wrote', rel, data.length, 'bytes')
}

const ANY = { radius: 112, scale: 1 }
const MASKABLE = { radius: 0, scale: 0.78 }
write('favicon.svg', svg(ANY))
write('icons/icon.svg', svg(ANY))
write('icons/icon-192.png', render(192, ANY))
write('icons/icon-512.png', render(512, ANY))
write('icons/maskable-192.png', render(192, MASKABLE))
write('icons/maskable-512.png', render(512, MASKABLE))
write('icons/apple-touch-icon.png', render(180, { radius: 0, scale: 0.86 }))
