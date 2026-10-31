import { defineConfig, loadEnv, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'

// Backend (FastAPI) paths. The SPA owns everything else (/, /j/:id, /share, /a/:id, /about).
const API_PREFIXES = ['/verify', '/report', '/legal', '/audit', '/health', '/admin', '/upload']
const BACKEND = process.env.VERILOOP_API ?? 'http://localhost:8000'

const proxy: Record<string, ProxyOptions> = Object.fromEntries(
  API_PREFIXES.map((p) => [p, { target: BACKEND, changeOrigin: false, ws: false }]),
)

export default defineConfig(({ command, mode }) => {
  // Mock fixtures may only ship in dev, or when a build explicitly sets VITE_MOCK=1 (src/lib/mockMode.ts).
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const mockAllowed = command === 'serve' || env.VITE_MOCK === '1'

  return {
    define: { __MOCK_ALLOWED__: JSON.stringify(mockAllowed) },
    plugins: [
      react(),
      tailwindcss(),
      VitePWA({
        registerType: 'autoUpdate',
        injectRegister: 'auto',
        includeAssets: ['favicon.svg', 'icons/apple-touch-icon.png', 'icons/icon.svg'],
        manifest: {
          id: '/',
          name: 'VeriLoop: verify, report, audit',
          short_name: 'VeriLoop',
          description:
            'Check a viral claim or video: cited verdict or honest abstain, evidence bundle, platform report, legal draft and a tamper-evident audit record.',
          lang: 'en-IN',
          start_url: '/',
          scope: '/',
          display: 'standalone',
          background_color: '#000000',
          theme_color: '#000000',
          categories: ['news', 'utilities', 'productivity'],
          icons: [
            { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
            { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
            { src: '/icons/maskable-192.png', sizes: '192x192', type: 'image/png', purpose: 'maskable' },
            { src: '/icons/maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
            { src: '/icons/icon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any' },
          ],
          // Android "Share -> VeriLoop". Many apps put the link inside `text`; /share handles both.
          share_target: {
            action: '/share',
            method: 'GET',
            params: { title: 'title', text: 'text', url: 'url' },
          },
        },
        workbox: {
          globPatterns: ['**/*.{js,css,html,svg,png,webmanifest}'],
          navigateFallback: '/index.html',
          // Never answer API navigations (PDF, bundle.zip, pubkey, SSE) with the app shell.
          navigateFallbackDenylist: [
            /^\/(?:verify|report|legal|audit|health|admin|upload[^/?#]*|docs|redoc|openapi\.json)(?:[/?#]|$)/,
          ],
          cleanupOutdatedCaches: true,
        },
      }),
    ],
    server: { port: 5173, proxy },
    preview: { port: 4173, proxy },
    build: { outDir: 'dist', target: 'es2022', sourcemap: false },
  }
})
