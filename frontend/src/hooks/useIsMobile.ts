import { useEffect, useState } from 'react'

// Below this width the three-pane / sidebar layouts (Forge, Profile,
// Debates) stop having room for two columns side by side — see the E2E-02/
// 03/04 mobile-layout fix report. 720 sits between phone-landscape (~740+)
// and tablet-portrait, matching the 390px-viewport repro used to file those
// bugs while staying clear of real tablet widths.
const DEFAULT_BREAKPOINT = 720

/**
 * Tracks whether the viewport is at or below `breakpointPx` wide, live.
 * Mirrors useReducedMotion's shape: the initial value is read via
 * `matchMedia` in the useState initializer (SSR/jsdom-safe — `matchMedia`
 * may be undefined there), and the effect only subscribes to subsequent
 * "change" events, falling back to the legacy `addListener` API for older
 * Safari. Degrades to `false` (desktop layout) when `matchMedia` is
 * unavailable so callers never crash.
 */
export function useIsMobile(breakpointPx: number = DEFAULT_BREAKPOINT): boolean {
  const query = `(max-width: ${breakpointPx}px)`
  const [isMobile, setIsMobile] = useState<boolean>(
    () => window.matchMedia?.(query).matches ?? false,
  )

  useEffect(() => {
    const mql = window.matchMedia?.(query)
    if (!mql) return
    // Initial value already read in the useState initializer (and the
    // query may have changed since then if breakpointPx changed) — sync
    // once on effect setup, then only subscribe to live changes.
    setIsMobile(mql.matches)
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches)
    if (mql.addEventListener) {
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    }
    // Legacy Safari (<14): addListener/removeListener.
    mql.addListener(onChange)
    return () => mql.removeListener(onChange)
  }, [query])

  return isMobile
}
