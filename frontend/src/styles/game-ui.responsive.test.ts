/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Source-level guard: jsdom does not evaluate imported CSS @media, so assert the
// three-viewport coverage exists in the stylesheet text. The Lab surface must
// have a mobile tier (<=680px, stacks the split), a tablet tier (681-1024px),
// and the desktop default. Real layout at each width is confirmed at runtime.
describe('game-ui.css — three-viewport responsive coverage for the Lab surface', () => {
  const css = readFileSync(resolve(process.cwd(), 'src/styles/game-ui.css'), 'utf8')

  it('keeps the mobile tier that stacks the lab split (<=680px)', () => {
    const mobile = css.match(/@media\s*\(max-width:\s*680px\)\s*\{[\s\S]*?\n\}/)?.[0] ?? ''
    expect(mobile).toMatch(/\.game-lab-split\s*\{\s*flex-direction:\s*column/)
  })

  it('adds a tablet tier (681-1024px) that targets a Lab selector', () => {
    const tablet = css.match(
      /@media\s*\(min-width:\s*681px\)\s*and\s*\(max-width:\s*1024px\)\s*\{[\s\S]*?\n\}/,
    )?.[0] ?? ''
    expect(tablet).not.toBe('')
    expect(tablet).toMatch(/\.game-lab-split|\.game-modal-panel/)
  })
})
