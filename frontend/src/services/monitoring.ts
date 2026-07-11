/**
 * Sentry wiring (Phase 3 可观测性), bundle-conscious:
 * @sentry/react is only ever loaded via dynamic import and only when
 * VITE_SENTRY_DSN is configured — so it lands in its own async chunk and the
 * P1-4 first-load budget (~233 kB entry) is untouched when Sentry is off.
 */

const DSN: string | undefined = import.meta.env.VITE_SENTRY_DSN

// The dynamic-import promise doubles as the "initialized" latch.
let sentryLoad: Promise<typeof import('@sentry/react')> | null = null

export function initMonitoring(): void {
  if (!DSN || sentryLoad) return
  sentryLoad = import('@sentry/react')
  sentryLoad
    .then((Sentry) => {
      Sentry.init({
        dsn: DSN,
        environment: import.meta.env.MODE,
        // Error monitoring only; perf tracing stays off until someone needs it.
        tracesSampleRate: 0,
      })
    })
    .catch((err) => {
      console.warn('[monitoring] Sentry failed to load', err)
      sentryLoad = null
    })
}

/** Report a caught error (no-op when Sentry is not configured). */
export function captureError(error: unknown): void {
  if (!sentryLoad) return
  sentryLoad.then((Sentry) => Sentry.captureException(error)).catch(() => {})
}
