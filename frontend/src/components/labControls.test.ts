import { describe, expect, it } from 'vitest'
import type { CSSProperties } from 'react'
import { labInput, labTab, labChip, labPublishBtn, labTaskRow, labBtn, labClose } from './labControls'

const MIN = 44

function h(s: CSSProperties): number {
  return typeof s.minHeight === 'number' ? s.minHeight : Number(s.minHeight)
}
function w(s: CSSProperties): number {
  return typeof s.minWidth === 'number' ? s.minWidth : Number(s.minWidth)
}

describe('labControls — 44px minimum touch targets (WCAG 2.5.5 / iOS HIG)', () => {
  it('every interactive control clears 44px min height', () => {
    expect(h(labInput)).toBeGreaterThanOrEqual(MIN)
    expect(h(labTab(true))).toBeGreaterThanOrEqual(MIN)
    expect(h(labTab(false))).toBeGreaterThanOrEqual(MIN)
    expect(h(labChip(true))).toBeGreaterThanOrEqual(MIN)
    expect(h(labChip(false))).toBeGreaterThanOrEqual(MIN)
    expect(h(labPublishBtn(false))).toBeGreaterThanOrEqual(MIN)
    expect(h(labPublishBtn(true))).toBeGreaterThanOrEqual(MIN)
    expect(h(labTaskRow(true))).toBeGreaterThanOrEqual(MIN)
    expect(h(labTaskRow(false))).toBeGreaterThanOrEqual(MIN)
    expect(h(labBtn('#14b8a6'))).toBeGreaterThanOrEqual(MIN)
    expect(h(labClose())).toBeGreaterThanOrEqual(MIN)
  })

  it('the close button is a square target (min width too)', () => {
    expect(w(labClose())).toBeGreaterThanOrEqual(MIN)
  })

  it('preserves the accent styling contract (active tab / chip / btn color)', () => {
    expect(labTab(true).color).toBe('#14b8a6')
    expect(labChip(true).color).toBe('#14b8a6')
    expect(labBtn('#ef4444').color).toBe('#ef4444')
    expect(labPublishBtn(true).opacity).toBe(0.6)
    expect(labPublishBtn(false).opacity).toBe(1)
  })
})
