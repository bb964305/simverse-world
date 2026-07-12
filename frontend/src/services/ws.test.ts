import { describe, it, expect } from 'vitest'
import { computeBackoffDelay } from './ws'
import { useGameStore } from '../stores/gameStore'

describe('computeBackoffDelay (P2 exponential backoff)', () => {
  it('attempt 0 starts around 3s within ±20% jitter', () => {
    for (let i = 0; i < 50; i++) {
      const d = computeBackoffDelay(0)
      expect(d).toBeGreaterThanOrEqual(3000 * 0.8)
      expect(d).toBeLessThanOrEqual(3000 * 1.2)
    }
  })

  it('doubles per attempt: attempt 1 ≈ 6s, attempt 2 ≈ 12s (±20%)', () => {
    for (let i = 0; i < 50; i++) {
      const d1 = computeBackoffDelay(1)
      expect(d1).toBeGreaterThanOrEqual(6000 * 0.8)
      expect(d1).toBeLessThanOrEqual(6000 * 1.2)
      const d2 = computeBackoffDelay(2)
      expect(d2).toBeGreaterThanOrEqual(12000 * 0.8)
      expect(d2).toBeLessThanOrEqual(12000 * 1.2)
    }
  })

  it('caps the base at 30s (jitter may push to at most 36s)', () => {
    for (const attempt of [4, 5, 10, 30]) {
      for (let i = 0; i < 50; i++) {
        const d = computeBackoffDelay(attempt)
        expect(d).toBeGreaterThanOrEqual(30000 * 0.8)
        expect(d).toBeLessThanOrEqual(30000 * 1.2)
      }
    }
  })

  it('attempt 3 is 24s base (still below cap), attempt 4 hits the 30s cap', () => {
    const d3 = computeBackoffDelay(3)
    expect(d3).toBeLessThanOrEqual(24000 * 1.2)
    const d4 = computeBackoffDelay(4)
    expect(d4).toBeGreaterThanOrEqual(30000 * 0.8)
  })
})

describe('wsStatus slice', () => {
  it('defaults to connected and toggles via setWsStatus', () => {
    expect(useGameStore.getState().wsStatus).toBe('connected')
    useGameStore.getState().setWsStatus('reconnecting')
    expect(useGameStore.getState().wsStatus).toBe('reconnecting')
    useGameStore.getState().setWsStatus('connected')
    expect(useGameStore.getState().wsStatus).toBe('connected')
  })
})
