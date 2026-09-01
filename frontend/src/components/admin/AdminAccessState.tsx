import { Link } from 'react-router-dom'
import type { CSSProperties } from 'react'
import { useLocale } from '../../services/locale'

const actionStyle: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  minHeight: 40,
  padding: '0 16px',
  border: '1px solid var(--border)',
  borderRadius: 8,
  color: 'var(--text-primary)',
  background: 'var(--bg-card)',
  font: 'inherit',
  textDecoration: 'none',
  cursor: 'pointer',
}

interface AdminAccessStateProps {
  kind: 'forbidden' | 'verification_error'
  onRetry?: () => void
}

export function AdminAccessState({ kind, onRetry }: AdminAccessStateProps) {
  const verificationFailed = kind === 'verification_error'
  const locale = useLocale((state) => state.locale)
  const zh = locale === 'zh-CN'

  return (
    <main
      aria-labelledby="admin-access-title"
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        padding: 24,
        boxSizing: 'border-box',
        color: 'var(--text-primary)',
        background: 'var(--bg-page)',
      }}
    >
      <section style={{ width: 'min(440px, 100%)', textAlign: 'center' }}>
        <div style={{ fontSize: 32, marginBottom: 16 }} aria-hidden="true">
          {verificationFailed ? '◌' : '🔒'}
        </div>
        <h1 id="admin-access-title" style={{ fontSize: 24, marginBottom: 12 }}>
          {verificationFailed
            ? (zh ? '暂时无法验证后台权限' : 'Unable to verify admin access')
            : (zh ? '没有后台管理权限' : 'Admin access required')}
        </h1>
        <p style={{ color: 'var(--text-secondary)', lineHeight: 1.7, marginBottom: 24 }}>
          {verificationFailed
            ? (zh
                ? '身份服务暂时不可用。为保护管理数据，本页不会使用本地缓存放行。'
                : 'The identity service is temporarily unavailable. Cached credentials will not be used to expose admin data.')
            : (zh
                ? '当前账号已登录，但未被授予后台管理权限。'
                : 'This account is signed in but has not been granted administrator access.')}
        </p>
        <div style={{ display: 'flex', justifyContent: 'center', gap: 12 }}>
          {verificationFailed && onRetry && (
            <button type="button" style={{ ...actionStyle, borderColor: 'var(--accent-blue)' }} onClick={onRetry}>
              {zh ? '重新验证' : 'Verify again'}
            </button>
          )}
          <Link style={actionStyle} to="/">
            {zh ? '返回首页' : 'Back to home'}
          </Link>
        </div>
      </section>
    </main>
  )
}
