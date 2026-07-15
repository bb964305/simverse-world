import { Component, type ReactNode } from 'react'
import { captureError } from '../services/monitoring'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Route-level error boundary: a crash inside any page (Phaser included) shows
 * a recoverable fallback instead of white-screening the whole app.
 * "重试" re-renders in place; "回到首页" does a hard reload so Phaser/WS
 * singletons restart from a clean slate.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error) {
    console.error('[ErrorBoundary]', error)
    captureError(error) // no-op unless VITE_SENTRY_DSN is configured
  }

  handleRetry = () => {
    this.setState({ error: null })
  }

  handleGoHome = () => {
    window.location.href = '/play'
  }

  render() {
    if (!this.state.error) return this.props.children
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
        <div style={{ fontSize: 18, fontWeight: 600 }}>页面出错了</div>
        <div style={{ color: 'var(--text-muted)', fontSize: 13, maxWidth: 420, textAlign: 'center' }}>
          {this.state.error.message}
        </div>
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
            重试
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
            回到首页
          </button>
        </div>
      </div>
    )
  }
}
