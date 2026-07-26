import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Kept separate from vite.config.ts so the build pipeline (manualChunks,
// visualizer) stays untouched; vitest picks this file up with priority.
export default defineConfig({
  plugins: [react()],
  define: {
    __RESIDENT_SPRITE_ASSET_VERSION__: JSON.stringify('legacy-blocked'),
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // Stores/api tests touch localStorage + module-level state; keep files
    // isolated from each other but fast.
    clearMocks: true,
  },
})
