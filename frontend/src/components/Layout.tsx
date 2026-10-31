import { useEffect, useState } from 'react'
import { NavLink, Outlet, Link } from 'react-router'
import { MOCK } from '../lib/mockMode'
import { isExtractive, isReplay, useHealth } from '../hooks/useHealth'
import { HealthBadge } from './HealthBadge'
import { Icon } from './Icon'

export function MockBanner() {
  if (!MOCK) return null
  return (
    <div role="note" className="border-b-2 border-mock-fg bg-mock-bg text-mock-fg">
      <div className="mock-stripes h-1.5" aria-hidden="true" />
      <p className="mx-auto max-w-5xl px-4 py-1.5 text-sm font-bold">
        MOCK DATA: fictional fixtures served in the browser, not real evidence. All links point to example.org.
      </p>
    </div>
  )
}

/** Prominent banners for backend modes that change what the user is looking at. */
export function ModeBanners() {
  const { health } = useHealth()
  if (!health) return null
  const replay = isReplay(health.mode)
  const extractive = isExtractive(health)
  const record = (health.mode ?? '').toLowerCase() === 'record'
  if (!replay && !extractive && !record) return null
  return (
    <div className="flex flex-col">
      {replay ? (
        <div role="note" className="bg-replay-bg text-replay-fg">
          <p className="mx-auto flex max-w-5xl items-center gap-2 px-4 py-2 text-sm">
            <Icon name="activity" />
            <span>
              <strong className="tracking-wide">REPLAY MODE</strong>: external tool responses are replayed from recorded
              cassettes, not fetched live.
            </span>
          </p>
        </div>
      ) : null}
      {record ? (
        <div role="note" className="bg-primary-soft text-fg">
          <p className="mx-auto max-w-5xl px-4 py-1.5 text-sm">
            <strong>RECORD MODE</strong>: live tool responses are being recorded for replay.
          </p>
        </div>
      ) : null}
      {extractive ? (
        <div role="note" className="border-b border-abstain-line bg-abstain-soft text-abstain-fg">
          <p className="mx-auto flex max-w-5xl items-center gap-2 px-4 py-2 text-sm">
            <Icon name="info" />
            <span>
              <strong>LLM disabled (extractive mode)</strong>: no AI-written explanations; rationales are quoted evidence
              sentences with citations.
            </span>
          </p>
        </div>
      ) : null}
    </div>
  )
}

function OfflineBanner() {
  const [online, setOnline] = useState(() => (typeof navigator === 'undefined' ? true : navigator.onLine))
  useEffect(() => {
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    return () => {
      window.removeEventListener('online', on)
      window.removeEventListener('offline', off)
    }
  }, [])
  if (online) return null
  return (
    <div role="status" className="bg-surface-2 text-fg">
      <p className="mx-auto flex max-w-5xl items-center gap-2 px-4 py-1.5 text-sm">
        <Icon name="wifi_off" size={16} /> You are offline. The app works, but verification needs a connection.
      </p>
    </div>
  )
}

const navCls = ({ isActive }: { isActive: boolean }) =>
  `rounded-md px-3 py-2 text-sm font-semibold no-underline ${isActive ? 'bg-primary-soft text-fg' : 'text-muted hover:text-fg'}`

export function Layout() {
  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#main"
        className="sr-only z-50 rounded bg-surface px-3 py-2 focus:not-sr-only focus:absolute focus:left-2 focus:top-2"
      >
        Skip to content
      </a>
      <MockBanner />
      <ModeBanners />
      <OfflineBanner />
      <header className="sticky top-0 z-30 border-b border-border bg-surface/95 backdrop-blur supports-[backdrop-filter]:bg-surface/80">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-3 px-4 py-2">
          <Link to="/" className="flex items-center gap-2 text-fg no-underline hover:text-fg" aria-label="VeriLoop home">
            <img src="/favicon.svg" alt="" width={32} height={32} className="rounded-md" />
            <span className="text-lg font-extrabold tracking-tight">VeriLoop</span>
          </Link>
          <nav aria-label="Main" className="flex items-center gap-1">
            <NavLink to="/" end className={navCls}>
              Verify
            </NavLink>
            <NavLink to="/about" className={navCls}>
              How it works
            </NavLink>
          </nav>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-5xl flex-1 px-4 py-5 sm:py-8">
        <Outlet />
      </main>
      <footer className="border-t border-border bg-surface">
        <div className="mx-auto max-w-5xl space-y-3 px-4 py-4">
          <HealthBadge />
          <p className="text-xs text-muted">
            VeriLoop never submits reports or legal notices on your behalf. Every verdict is cited or it abstains, and
            every run is written to a signed, hash-chained audit log.
          </p>
        </div>
      </footer>
    </div>
  )
}
