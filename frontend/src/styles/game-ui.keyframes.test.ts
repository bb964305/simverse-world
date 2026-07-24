/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Source-level guard (jsdom does not evaluate imported CSS @keyframes/@media,
// and Vite's ?raw returns empty for CSS under vitest): read the stylesheet text
// directly. LabTimeline.tsx animates `sv-pulse` for the verifying phase and a
// resyncing connection; an undefined keyframe silently no-ops, so this asserts
// the definition exists. The real visual effect is verified at runtime (Step 6).
describe('game-ui.css — sv-pulse keyframe backing LabTimeline', () => {
  const css = readFileSync(resolve(process.cwd(), 'src/styles/game-ui.css'), 'utf8')

  it('defines the sv-pulse keyframe the timeline references', () => {
    expect(css).toMatch(/@keyframes\s+sv-pulse\s*\{/)
  })

  it('animates opacity so the pulse is actually visible', () => {
    const block = css.match(/@keyframes\s+sv-pulse\s*\{[^}]*\{[^}]*\}[^}]*\}/)?.[0] ?? ''
    expect(block).toMatch(/opacity/)
    expect(block).toMatch(/0?\.45/)
  })

  it('has a CSS reduced-motion backstop that disables sv-pulse (defense-in-depth beyond the JS hook)', () => {
    const reduced = css.match(/@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{[\s\S]*?\n\}/)?.[0] ?? ''
    expect(reduced).toMatch(/sv-pulse/)
    expect(reduced).toMatch(/animation:\s*none/)
  })
})
