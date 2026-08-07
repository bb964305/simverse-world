import { useCallback, useSyncExternalStore } from 'react'

// Below this width the three-pane / sidebar layouts (Forge, Profile,
// Debates) stop having room for two columns side by side — see the E2E-02/
// 03/04 mobile-layout fix report. 720 sits between phone-landscape (~740+)
// and tablet-portrait, matching the 390px-viewport repro used to file those
// bugs while staying clear of real tablet widths.
const DEFAULT_BREAKPOINT = 720

/**
 * Tracks whether the viewport is at or below `breakpointPx` wide, live.
 * The MediaQueryList is treated as an external store: useSyncExternalStore
 * reads the current value straight from `matchMedia` (SSR/jsdom-safe —
 * `matchMedia` may be undefined there) and re-reads it whenever the query's
 * "change" event fires, or when `breakpointPx` changes and swaps the
 * subscription. Falls back to the legacy `addListener` API for older Safari.
 * Degrades to `false` (desktop layout) when `matchMedia` is unavailable so
 * callers never crash.
 */
export function useIsMobile(breakpointPx: number = DEFAULT_BREAKPOINT): boolean {
  const query = `(max-width: ${breakpointPx}px)`
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      const mql = window.matchMedia?.(query)
      if (!mql) return () => {}
      if (mql.addEventListener) {
        mql.addEventListener('change', onStoreChange)
        return () => mql.removeEventListener('change', onStoreChange)
      }
      // Legacy Safari (<14): addListener/removeListener.
      mql.addListener(onStoreChange)
      return () => mql.removeListener(onStoreChange)
    },
    [query],
  )
  return useSyncExternalStore(subscribe, () => window.matchMedia?.(query).matches ?? false)
}
