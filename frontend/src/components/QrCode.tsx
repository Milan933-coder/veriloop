import { useMemo } from 'react'
import { encode } from 'uqr'

/** QR code as an SVG path (dark modules on a white quiet zone, so it scans in dark mode too). */
export function QrCode({ value, size = 168, label }: { value: string; size?: number; label: string }) {
  const { path, n } = useMemo(() => {
    const qr = encode(value, { ecc: 'M', border: 2 })
    let d = ''
    qr.data.forEach((row, y) =>
      row.forEach((on, x) => {
        if (on) d += `M${x} ${y}h1v1h-1z`
      }),
    )
    return { path: d, n: qr.size }
  }, [value])
  return (
    <svg viewBox={`0 0 ${n} ${n}`} width={size} height={size} role="img" aria-label={label} shapeRendering="crispEdges" className="rounded-md">
      <rect width={n} height={n} fill="#ffffff" />
      <path d={path} fill="#0f172a" />
    </svg>
  )
}
