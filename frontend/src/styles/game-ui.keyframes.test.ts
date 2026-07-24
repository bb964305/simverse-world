import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Source-level guard (jsdom does not evaluate imported CSS @keyframes/@media):
// LabTimeline.tsx animates `sv-pulse` for the verifying phase and a resyncing
// connection. If the keyframe is undefined the animation silently no-ops, so
// this asserts the definition exists. The real visual effect is verified at
// runtime (Step 6, verify-before-done).
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
})
