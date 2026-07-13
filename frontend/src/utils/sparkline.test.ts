import { describe, expect, it } from 'vitest'
import { barRects, fillDailySeries, lineDots, linePath } from './sparkline'

const box = { width: 100, height: 50, pad: 0 }

describe('sparkline', () => {
  it('linePath maps min/max to bottom/top and skips null gaps', () => {
    const path = linePath([0, null, 10], box)
    // Two segments: pen up over the null → two M commands, no L
    expect(path).toBe('M 0 50 M 100 0')
    const joined = linePath([0, 10], box)
    expect(joined).toBe('M 0 50 L 100 0')
  })

  it('linePath honors an explicit domain', () => {
    // rating 3 inside a fixed 1..5 domain → vertical midpoint
    expect(linePath([3], box, { min: 1, max: 5 })).toBe('M 50 25')
  })

  it('linePath/lineDots handle empty and all-null input', () => {
    expect(linePath([], box)).toBe('')
    expect(linePath([null, null], box)).toBe('')
    expect(lineDots([null], box)).toEqual([])
  })

  it('barRects scales heights from a zero baseline', () => {
    const [zero, half, full] = barRects([0, 5, 10], box)
    expect(full.height).toBe(50) // max value spans the full box
    expect(half.height).toBe(25)
    expect(zero.height).toBe(1) // zero value still renders a 1px stub
    expect(zero.y).toBe(49) // stub sits just above the baseline
    expect(full.y).toBeLessThan(half.y)
  })

  it('fillDailySeries zero-fills the window in date order', () => {
    const values = fillDailySeries(
      [{ date: '2026-07-02', value: 3 }, { date: '2026-07-04', value: 1 }],
      '2026-07-01',
      5,
    )
    expect(values).toEqual([0, 3, 0, 1, 0])
    expect(fillDailySeries([], 'not-a-date', 5)).toEqual([])
  })
})
