import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { visualizer } from 'rollup-plugin-visualizer'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    // Emit a bundle treemap (dist/stats.html) for size auditing when ANALYZE is set.
    // Kept off by default so normal `npm run build` (CI/deploy) stays clean.
    process.env.ANALYZE
      ? visualizer({ filename: 'dist/stats.html', gzipSize: true, brotliSize: true })
      : undefined,
  ],
  build: {
    rollupOptions: {
      output: {
        // Split the largest third-party deps into their own chunks so the
        // login/first-load bundle no longer ships the game engine or markdown
        // editor. Phaser (~1.4MB) is the mandatory split; md-editor rides its
        // own chunk too since it's only reachable from the profile page.
        // Function form (not the object form) is required: this project builds
        // on rolldown-vite, which only accepts a manualChunks(id) callback.
        manualChunks(id) {
          if (id.includes('node_modules/phaser')) return 'phaser'
        },
      },
    },
  },
})
