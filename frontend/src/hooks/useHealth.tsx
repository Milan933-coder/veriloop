import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { api } from '../api/client'
import { errorMessage } from '../api/errors'
import type { HealthView } from '../api/types'

interface HealthState {
  health: HealthView | null
  error: string | null
  loading: boolean
  refresh: () => void
}

const HealthContext = createContext<HealthState>({ health: null, error: null, loading: true, refresh: () => {} })

const REFRESH_MS = 60_000

/** GET /health once at start and every minute; shared by the footer badge and mode banners. */
export function HealthProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<HealthView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)

  const refresh = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    let alive = true
    setLoading(true)
    api
      .health()
      .then((h) => {
        if (!alive) return
        setHealth(h)
        setError(null)
      })
      .catch((e: unknown) => {
        if (alive) setError(errorMessage(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    const t = setTimeout(refresh, REFRESH_MS)
    return () => {
      alive = false
      clearTimeout(t)
    }
  }, [tick, refresh])

  return <HealthContext.Provider value={{ health, error, loading, refresh }}>{children}</HealthContext.Provider>
}

export function useHealth(): HealthState {
  return useContext(HealthContext)
}

export function isReplay(mode: string | null | undefined): boolean {
  return (mode ?? '').toLowerCase() === 'replay'
}

export function isExtractive(h: HealthView | null): boolean {
  return !!h && (h.llm_provider ?? '').toLowerCase() === 'none'
}
