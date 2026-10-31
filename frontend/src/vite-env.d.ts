/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_MOCK?: string
  readonly VITE_API_BASE?: string
  readonly VITE_API_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

/** Compile-time flag (vite.config.ts `define`): true in dev or when VITE_MOCK=1, so fixtures can be tree-shaken. */
declare const __MOCK_ALLOWED__: boolean
