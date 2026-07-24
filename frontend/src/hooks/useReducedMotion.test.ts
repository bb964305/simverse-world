import { describe, expect, it, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useReducedMotion } from './useReducedMotion'

// Build a controllable matchMedia mock whose change-listeners we can fire.
function stubMatchMedia(initialMatches: boolean) {
  const listeners = new Set<(e: MediaQueryListEvent) => void>()
  const mql = {
    matches: initialMatches,
    media: '(prefers-reduced-motion: reduce)',
    onchange: null,
    addEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => listeners.delete(cb),
    // legacy Safari fallback the hook also supports
    addListener: (cb: (e: MediaQueryListEvent) => void) => listeners.add(cb),
    removeListener: (cb: (e: MediaQueryListEvent) => void) => listeners.delete(cb),
    dispatchEvent: () => true,
  }
  const fire = (matches: boolean) => {
    mql.matches = matches
    listeners.forEach((cb) => cb({ matches } as MediaQueryListEvent))
  }
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue(mql))
  return { fire }
}

afterEach(() => vi.unstubAllGlobals())

describe('useReducedMotion', () => {
  it('returns true when the OS prefers reduced motion', () => {
    stubMatchMedia(true)
    const { result } = renderHook(() => useReducedMotion())
    expect(result.current).toBe(true)
  })

  it('returns false when the OS does not prefer reduced motion', () => {
    stubMatchMedia(false)
    const { result } = renderHook(() => useReducedMotion())
    expect(result.current).toBe(false)
  })

  it('reacts to a live change event', () => {
    const { fire } = stubMatchMedia(false)
    const { result } = renderHook(() => useReducedMotion())
    expect(result.current).toBe(false)
    act(() => fire(true))
    expect(result.current).toBe(true)
    act(() => fire(false))
    expect(result.current).toBe(false)
  })

  it('degrades to false when matchMedia is unavailable', () => {
    vi.stubGlobal('matchMedia', undefined)
    const { result } = renderHook(() => useReducedMotion())
    expect(result.current).toBe(false)
  })
})
