import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { visualizer } from 'rollup-plugin-visualizer'
import { existsSync, readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

const residentBatchReceipt = new URL(
  './public/assets/village/agents/generation-batch.json',
  import.meta.url,
)

function residentSpriteAssetVersion(): string {
  if (!existsSync(residentBatchReceipt)) return 'legacy-blocked'
  try {
    const value = JSON.parse(readFileSync(residentBatchReceipt, 'utf8')) as { batch_id?: unknown }
    return typeof value.batch_id === 'string' && /^[0-9a-f]{32}$/.test(value.batch_id)
      ? value.batch_id
      : 'invalid-batch'
  } catch {
    return 'invalid-batch'
  }
}

const caravanAssetInputs = [
  'merchant/generation-provenance.json',
  'merchant/texture.png',
  'merchant/atlas.json',
  'convoy/generation-provenance.json',
  'convoy/texture.png',
  'convoy/atlas.json',
  'stall/generation-provenance.json',
  'stall/texture.png',
].map((path) => new URL(`./public/assets/village/caravan/${path}`, import.meta.url))

function caravanAssetVersion(): string {
  if (caravanAssetInputs.some((path) => !existsSync(path))) return 'caravan-assets-missing'
  try {
    const digest = createHash('sha256')
    for (const path of caravanAssetInputs) digest.update(readFileSync(path))
    return digest.digest('hex').slice(0, 24)
  } catch {
    return 'caravan-assets-invalid'
  }
}

// https://vite.dev/config/
export default defineConfig({
  define: {
    __RESIDENT_SPRITE_ASSET_VERSION__: JSON.stringify(residentSpriteAssetVersion()),
    __CARAVAN_ASSET_VERSION__: JSON.stringify(caravanAssetVersion()),
  },
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
