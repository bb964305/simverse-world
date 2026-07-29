import { describe, expect, it, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useIsMobile } from './useIsMobile'

// Build a controllable matchMedia mock whose change-listeners we can fire.
// Mirrors the fixture in useReducedMotion.test.ts.
function stubMatchMedia(initialMatches: boolean) {
  const listeners = new Set<(e: MediaQueryListEvent) => void>()
  let lastQuery = ''
  const mql = {
    matches: initialMatches,
    media: '',
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
  const matchMediaSpy = vi.fn().mockImplementation((query: string) => {
    lastQuery = query
    return mql
  })
  vi.stubGlobal('matchMedia', matchMediaSpy)
  return { fire, matchMediaSpy, getLastQuery: () => lastQuery }
}

afterEach(() => vi.unstubAllGlobals())

describe('useIsMobile', () => {
  it('returns true when the viewport is at or below the breakpoint', () => {
    stubMatchMedia(true)
    const { result } = renderHook(() => useIsMobile(720))
    expect(result.current).toBe(true)
  })

  it('returns false when the viewport is wider than the breakpoint', () => {
    stubMatchMedia(false)
    const { result } = renderHook(() => useIsMobile(720))
    expect(result.current).toBe(false)
  })

  it('defaults to the 720px breakpoint when none is given', () => {
    const { matchMediaSpy, getLastQuery } = stubMatchMedia(false)
    renderHook(() => useIsMobile())
    expect(matchMediaSpy).toHaveBeenCalled()
    expect(getLastQuery()).toBe('(max-width: 720px)')
  })

  it('honors a custom breakpoint', () => {
    const { getLastQuery } = stubMatchMedia(false)
    renderHook(() => useIsMobile(480))
    expect(getLastQuery()).toBe('(max-width: 480px)')
  })

  it('reacts to a live change event (e.g. rotating / resizing the viewport)', () => {
    const { fire } = stubMatchMedia(false)
    const { result } = renderHook(() => useIsMobile(720))
    expect(result.current).toBe(false)
    act(() => fire(true))
    expect(result.current).toBe(true)
    act(() => fire(false))
    expect(result.current).toBe(false)
  })

  it('removes its change listener on unmount', () => {
    const listeners = new Set<(e: MediaQueryListEvent) => void>()
    const mql = {
      matches: false,
      media: '',
      onchange: null,
      addEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => listeners.add(cb),
      removeEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => listeners.delete(cb),
      addListener: (cb: (e: MediaQueryListEvent) => void) => listeners.add(cb),
      removeListener: (cb: (e: MediaQueryListEvent) => void) => listeners.delete(cb),
      dispatchEvent: () => true,
    }
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue(mql))

    const { unmount } = renderHook(() => useIsMobile(720))
    expect(listeners.size).toBe(1)
    unmount()
    expect(listeners.size).toBe(0)
  })

  it('degrades to false (desktop layout) when matchMedia is unavailable', () => {
    vi.stubGlobal('matchMedia', undefined)
    const { result } = renderHook(() => useIsMobile(720))
    expect(result.current).toBe(false)
  })
})
