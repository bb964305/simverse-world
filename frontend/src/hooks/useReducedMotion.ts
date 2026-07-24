import { useEffect, useState } from 'react'

const QUERY = '(prefers-reduced-motion: reduce)'

/**
 * Tracks the OS `prefers-reduced-motion` setting, live. Returns false when
 * `matchMedia` is unavailable (jsdom, very old runtimes) so callers degrade to
 * full motion rather than crashing — mirrors the optional-chaining pattern
 * already used in LandingPage. Subscribes with `addEventListener`, falling back
 * to the legacy `addListener` (older Safari) so the value updates when the user
 * flips the OS toggle without a reload.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(
    () => window.matchMedia?.(QUERY).matches ?? false,
  )

  useEffect(() => {
    const mql = window.matchMedia?.(QUERY)
    if (!mql) return
    // Initial value already read in the useState initializer; only subscribe to
    // subsequent OS-toggle changes here (avoids a redundant setState in effect).
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches)
    if (mql.addEventListener) {
      mql.addEventListener('change', onChange)
      return () => mql.removeEventListener('change', onChange)
    }
    // Legacy Safari (<14): addListener/removeListener.
    mql.addListener(onChange)
    return () => mql.removeListener(onChange)
  }, [])

  return reduced
}
