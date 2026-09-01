import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useGameStore } from '../stores/gameStore'
import {
  clearOAuthReturnTo,
  loginPath,
  onboardingPath,
  readOAuthReturnTo,
} from '../services/authReturnTo'
import { useLocale } from '../services/locale'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export function AuthCallbackPage() {
  const en = useLocale((state) => state.locale === 'en')
  // The callback URL never changes while this page is mounted, so read the
  // token once (lazy init) and derive the initial error from it — this
  // replaces the old synchronous setError inside the effect
  // (react-hooks/set-state-in-effect) with identical rendered output.
  const [token] = useState(() => new URLSearchParams(window.location.search).get('token'))
  const [next] = useState(() => readOAuthReturnTo())
  const [error, setError] = useState(token ? '' : (en ? 'Sign-in failed: no token was received.' : '登录失败：未收到 token'))
  const setAuth = useGameStore((s) => s.setAuth)
  const navigate = useNavigate()

  useEffect(() => {
    let cancelled = false
    let redirectTimer: ReturnType<typeof setTimeout> | undefined
    const controller = new AbortController()
    const returnToLogin = () => {
      redirectTimer = setTimeout(() => navigate(loginPath(next), { replace: true }), 2000)
    }

    if (!token) {
      returnToLogin()
      return () => {
        cancelled = true
        controller.abort()
        if (redirectTimer) clearTimeout(redirectTimer)
      }
    }

    const fetchUser = async () => {
      try {
        const resp = await fetch(`${API}/users/me`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        })
        if (cancelled) return
        if (!resp.ok) {
          setError(en ? 'Sign-in failed: your wallet identity could not be loaded.' : '登录失败：无法获取用户信息')
          returnToLogin()
          return
        }
        const user = await resp.json()
        if (cancelled) return
        clearOAuthReturnTo()
        setAuth(user, token)
        navigate(onboardingPath(next), { replace: true })
      } catch (reason: unknown) {
        if (cancelled || (reason instanceof DOMException && reason.name === 'AbortError')) return
        setError(en ? 'Network error. Please try again.' : '网络错误，请重试')
        returnToLogin()
      }
    }

    void fetchUser()
    return () => {
      cancelled = true
      controller.abort()
      if (redirectTimer) clearTimeout(redirectTimer)
    }
  }, [token, next, navigate, setAuth, en])

  return (
    <div style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'linear-gradient(135deg, #1a1a2e 0%, #0f3460 100%)',
    }}>
      <div style={{
        background: '#18181bf0', border: '1px solid var(--border)', borderRadius: 16,
        padding: 32, width: 340, backdropFilter: 'blur(12px)', textAlign: 'center',
      }}>
        {error ? (
          <>
            <div style={{ fontSize: 28, marginBottom: 12 }}>⚠️</div>
            <div style={{ color: 'var(--accent-red)', fontSize: 14, marginBottom: 8 }}>{error}</div>
            <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>{en ? 'Returning to sign in…' : '正在跳转到登录页…'}</div>
          </>
        ) : (
          <>
            <div style={{ fontSize: 28, marginBottom: 12 }}>🏙️</div>
            <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 8 }}>{en ? 'Signing in…' : '正在登录中…'}</div>
            <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>{en ? 'Please wait' : '请稍候'}</div>
          </>
        )}
      </div>
    </div>
  )
}
