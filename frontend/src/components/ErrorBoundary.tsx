import { Component, type ReactNode } from 'react'
import { captureError } from '../services/monitoring'
import { useLocale } from '../services/locale'

interface Props {
  children: ReactNode
  en?: boolean
}

interface State {
  error: Error | null
}

// Cloudflare Workers Assets deploys are a full swap: the previous build's
// hashed chunk files disappear the instant a new version goes live, and
// `not_found_handling = "single-page-application"` makes the CDN answer
// those now-missing chunk URLs with `index.html` (200 OK, content-type
// text/html) instead of a 404. A tab that was already open before the
// deploy then tries to `import()` a chunk that "succeeds" at the network
// layer but hands the JS engine an HTML document, which throws instead of
// resolving. Browsers/bundlers word this error differently, so match
// loosely on the substrings they share (see E2E-05).
const CHUNK_RELOAD_FLAG = 'sv:chunk-reload-attempted'

function isDynamicImportFailure(error: Error | null | undefined): boolean {
  if (!error) return false
  if (error.name === 'ChunkLoadError') return true
  const msg = (error.message || '').toLowerCase()
  const substrings = [
    'failed to fetch dynamically imported module',
    'importing a module script failed',
    'error loading dynamically imported module',
    'chunkloaderror',
  ]
  if (substrings.some((s) => msg.includes(s))) return true
  // "Unexpected token '<'" (and similar) is what you get when the JS
  // engine tries to parse an HTML document (index.html) as a module.
  return msg.includes('unexpected token') && msg.includes('<')
}

/**
 * Route-level error boundary: a crash inside any page (Phaser included) shows
 * a recoverable fallback instead of white-screening the whole app.
 * "重试" re-renders in place; "回到首页" does a hard reload so Phaser/WS
 * singletons restart from a clean slate.
 *
 * A stale dynamic-import chunk (see isDynamicImportFailure above) is treated
 * specially: it's not a real bug in the running code, it's the running code
 * being outdated, so a single automatic `location.reload()` is attempted
 * before falling back to the generic error UI. A sessionStorage flag caps
 * this at one attempt per tab session so a persistently broken deploy can't
 * reload-loop the page; the flag is cleared again once the boundary mounts
 * healthily (no error), so a later, unrelated deploy can still self-heal.
 */
class ErrorBoundaryCore extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidMount() {
    if (!this.state.error) {
      this.clearReloadFlag()
    }
  }

  componentDidCatch(error: Error) {
    console.error('[ErrorBoundary]', error)
    captureError(error) // no-op unless VITE_SENTRY_DSN is configured

    if (isDynamicImportFailure(error) && !this.hasAlreadyAttemptedReload()) {
      this.markReloadAttempted()
      window.location.reload()
    }
  }

  private hasAlreadyAttemptedReload(): boolean {
    try {
      return sessionStorage.getItem(CHUNK_RELOAD_FLAG) === '1'
    } catch {
      // sessionStorage can throw in privacy/incognito modes; treat as
      // "already attempted" so we fail safe toward showing the error UI
      // rather than reload-looping.
      return true
    }
  }

  private markReloadAttempted() {
    try {
      sessionStorage.setItem(CHUNK_RELOAD_FLAG, '1')
    } catch {
      // ignore — see hasAlreadyAttemptedReload
    }
  }

  private clearReloadFlag() {
    try {
      sessionStorage.removeItem(CHUNK_RELOAD_FLAG)
    } catch {
      // ignore
    }
  }

  handleRetry = () => {
    this.setState({ error: null })
  }

  handleGoHome = () => {
    window.location.href = '/play'
  }

  render() {
    if (!this.state.error) return this.props.children
    const staleChunk = isDynamicImportFailure(this.state.error)
    const en = this.props.en === true
    return (
      <div
        role="alert"
        style={{
          height: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 16,
          background: 'var(--bg-page)',
          color: 'var(--text-primary)',
        }}
      >
        <div style={{ fontSize: 40 }}>💥</div>
        <div style={{ fontSize: 18, fontWeight: 600 }}>{en ? 'Something went wrong' : '页面出错了'}</div>
        <div style={{ color: 'var(--text-muted)', fontSize: 13, maxWidth: 420, textAlign: 'center' }}>
          {this.state.error.message}
        </div>
        {staleChunk && (
          <div style={{ color: 'var(--text-muted)', fontSize: 13, maxWidth: 420, textAlign: 'center' }}>
            {en ? 'A new version was detected, but automatic recovery failed. Refresh the page and try again.' : '检测到新版本已发布，自动刷新未能解决问题，请手动刷新页面重试。'}
          </div>
        )}
        <div style={{ display: 'flex', gap: 12 }}>
          <button
            onClick={this.handleRetry}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: '1px solid var(--border)',
              background: 'var(--bg-card)',
              color: 'var(--text-primary)',
              cursor: 'pointer',
            }}
          >
            {en ? 'Retry' : '重试'}
          </button>
          <button
            onClick={this.handleGoHome}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: 'none',
              background: 'var(--accent-red)',
              color: '#fff',
              cursor: 'pointer',
            }}
          >
            {en ? 'Return to world' : '回到首页'}
          </button>
        </div>
      </div>
    )
  }
}

export function ErrorBoundary({ children }: { children: ReactNode }) {
  const en = useLocale((state) => state.locale === 'en')
  return <ErrorBoundaryCore en={en}>{children}</ErrorBoundaryCore>
}
