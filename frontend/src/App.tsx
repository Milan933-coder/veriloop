import { Suspense, lazy, type ReactNode } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'
import { Layout } from './components/Layout'
import { Spinner } from './components/ui'
import { HealthProvider } from './hooks/useHealth'
import SubmitPage from './pages/SubmitPage'

// Heavier routes are split out so the first screen (submit) stays small.
const JobPage = lazy(() => import('./pages/JobPage'))
const SharePage = lazy(() => import('./pages/SharePage'))
const AuditPage = lazy(() => import('./pages/AuditPage'))
const AboutPage = lazy(() => import('./pages/AboutPage'))
const NotFoundPage = lazy(() => import('./pages/NotFoundPage'))

function Lazy({ children }: { children: ReactNode }) {
  return <Suspense fallback={<Spinner label="Loading…" />}>{children}</Suspense>
}

/** Client routes (BUILD_SPEC §9): /, /j/:id, /share, /a/:id, /about. FastAPI serves index.html for them. */
export function App() {
  return (
    <BrowserRouter>
      <HealthProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<SubmitPage />} />
            <Route path="j/:id" element={<Lazy><JobPage /></Lazy>} />
            <Route path="share" element={<Lazy><SharePage /></Lazy>} />
            <Route path="a/:id" element={<Lazy><AuditPage /></Lazy>} />
            <Route path="about" element={<Lazy><AboutPage /></Lazy>} />
            <Route path="*" element={<Lazy><NotFoundPage /></Lazy>} />
          </Route>
        </Routes>
      </HealthProvider>
    </BrowserRouter>
  )
}
