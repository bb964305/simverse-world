import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useGameStore } from '../stores/gameStore'
import './LandingPage.css'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'

function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//')) return '/play'
  return raw
}

export function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [isRegister, setIsRegister] = useState(false)
  const [error, setError] = useState('')
  const setAuth = useGameStore((s) => s.setAuth)
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))

  const submit = async () => {
    setError('')
    const endpoint = isRegister ? '/auth/register' : '/auth/login'
    const body = isRegister ? { name, email, password } : { email, password }
    try {
      const resp = await fetch(`${API}${endpoint}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}))
        const detail = err?.detail
        const msg = typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? detail.map((d) => d?.msg).filter(Boolean).join('；')
            : ''
        setError(msg || '操作失败')
        return
      }
      const data = await resp.json()
      setAuth(data.user, data.access_token)
      navigate(`/onboarding?next=${encodeURIComponent(next)}`)
    } catch {
      setError('网络错误，请重试')
    }
  }

  return (
    <div className="mkt mkt-auth">
      <div className="mkt-atmosphere" aria-hidden="true" />
      <div className="mkt-grid" aria-hidden="true" />
      <div className="mkt-auth-shell">
        <div className="mkt-auth-card">
          <div className="mkt-auth-brand">
            <Link to="/" className="mkt-brand" aria-label="返回首页">
              <span className="mkt-brand-mark" aria-hidden="true" />
              <span>Simverse World</span>
            </Link>
            <p>一座永不关闭的赛博城市</p>
          </div>

          <div className="mkt-auth-oauth">
            <a href={`${API}/auth/github/login`} className="mkt-auth-oauth-btn">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
              </svg>
              GitHub 登录
            </a>
            <a href={`${API}/auth/linuxdo/login`} className="mkt-auth-oauth-btn">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <circle cx="12" cy="12" r="9" />
                <path d="M8 14c1.2 1.5 2.5 2 4 2s2.8-.5 4-2" />
                <circle cx="9" cy="10" r="1" fill="currentColor" />
                <circle cx="15" cy="10" r="1" fill="currentColor" />
              </svg>
              LinuxDo 登录
            </a>
          </div>

          <div className="mkt-auth-divider">
            <span>或使用邮箱</span>
          </div>

          {isRegister && (
            <input
              className="mkt-auth-input"
              placeholder="名字"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="name"
            />
          )}
          <input
            className="mkt-auth-input"
            placeholder="邮箱"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
          />
          <input
            className="mkt-auth-input"
            placeholder="密码"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            autoComplete={isRegister ? 'new-password' : 'current-password'}
          />
          {error && <div className="mkt-auth-error">{error}</div>}
          <button type="button" className="mkt-btn mkt-btn-primary mkt-auth-submit" onClick={submit}>
            {isRegister ? '注册并进入城市' : '进入城市'}
          </button>
          <button
            type="button"
            className="mkt-auth-toggle"
            onClick={() => setIsRegister(!isRegister)}
          >
            {isRegister ? '已有账号？登录' : '没有账号？注册'}
          </button>
          <Link to="/" className="mkt-auth-back">
            返回官网
          </Link>
        </div>
      </div>
    </div>
  )
}
