import { describe, expect, it } from 'vitest'
import { parseUTC } from './time'

describe('parseUTC', () => {
  it('pins naive backend timestamps to UTC', () => {
    // Same instant regardless of the test runner's local timezone.
    expect(parseUTC('2026-07-13T08:00:00').getTime())
      .toBe(Date.parse('2026-07-13T08:00:00Z'))
    // Fractional seconds (Python isoformat default) survive.
    expect(parseUTC('2026-07-13T08:00:00.123456').getTime())
      .toBe(Date.parse('2026-07-13T08:00:00.123Z'))
  })

  it('leaves explicit timezone suffixes untouched', () => {
    expect(parseUTC('2026-07-13T08:00:00Z').getTime())
      .toBe(Date.parse('2026-07-13T08:00:00Z'))
    expect(parseUTC('2026-07-13T08:00:00+08:00').getTime())
      .toBe(Date.parse('2026-07-13T00:00:00Z'))
    expect(parseUTC('2026-07-13T08:00:00-05:30').getTime())
      .toBe(Date.parse('2026-07-13T13:30:00Z'))
  })

  it('yields Invalid Date for garbage input (callers guard with isNaN)', () => {
    expect(Number.isNaN(parseUTC('not-a-date').getTime())).toBe(true)
  })
})
