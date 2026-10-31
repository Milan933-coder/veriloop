import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Unit tests run in plain Node: the logic under test is pure, and components are checked with
// react-dom/server (no jsdom dependency needed).
export default defineConfig({
  plugins: [react()],
  define: { __MOCK_ALLOWED__: 'true' },
  test: {
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
