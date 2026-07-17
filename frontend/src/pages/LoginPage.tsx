import { useEffect, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useGameStore } from '../stores/gameStore'
import '../styles/login-page.css'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [isRegister, setIsRegister] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState('')
  const setAuth = useGameStore((state) => state.setAuth)
  const navigate = useNavigate()

  useEffect(() => {
    document.body.classList.add('auth-page-open')
    return () => document.body.classList.remove('auth-page-open')
  }, [])

  const submit = async () => {
    if (isSubmitting) return
    setError('')
    setIsSubmitting(true)
    const endpoint = isRegister ? '/auth/register' : '/auth/login'
    const body = isRegister ? { name, email, password } : { email, password }

    try {
      const response = await fetch(`${API}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!response.ok) {
        const responseBody = await response.json().catch(() => ({}))
        const detail = responseBody?.detail
        const message = typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? detail.map((item) => item?.msg).filter(Boolean).join('；')
            : ''
        setError(message || '操作失败')
        return
      }

      const data = await response.json()
      setAuth(data.user, data.access_token)
      navigate('/onboarding')
    } catch {
      setError('网络错误，请重试')
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    void submit()
  }

  return (
    <main className="auth-page">
      <img className="auth-page__backdrop" src="/marketing/world-hero.jpg" alt="" />
      <div className="auth-page__shade" />

      <header className="auth-header">
        <Link className="auth-brand" to="/" aria-label="返回 Simverse World 官网">
          <span className="auth-brand__mark" aria-hidden="true">S/</span>
          <span>SIMVERSE</span>
        </Link>
        <Link className="auth-header__back" to="/">返回官网</Link>
      </header>

      <section className="auth-story" aria-labelledby="auth-page-title">
        <p>YOUR CITY ACCESS / NODE 07</p>
        <h1 id="auth-page-title">回到一座<br />持续生活的城市。</h1>
        <span>居民、关系与记忆都在继续。登录后从上次离开的地方重新进入。</span>
      </section>

      <section className="auth-panel" aria-labelledby="auth-form-title">
        <div className="auth-mode" role="group" aria-label="认证模式">
          <button
            type="button"
            aria-pressed={!isRegister}
            onClick={() => { setIsRegister(false); setError('') }}
          >
            登录
          </button>
          <button
            type="button"
            aria-pressed={isRegister}
            onClick={() => { setIsRegister(true); setError('') }}
          >
            注册
          </button>
        </div>

        <div className="auth-panel__heading">
          <p>CITY ACCESS</p>
          <h2 id="auth-form-title">{isRegister ? '创建通行身份' : '进入 Simverse'}</h2>
          <span>{isRegister ? '完成注册后开始创建你的第一位居民。' : '选择一种方式继续进入城市。'}</span>
        </div>

        <div className="auth-oauth">
          <a href={`${API}/auth/github/login`}>
            <svg aria-hidden="true"><use href="/icons.svg#github-icon" /></svg>
            GitHub 登录
          </a>
          <a href={`${API}/auth/linuxdo/login`}>
            <span className="auth-oauth__linuxdo" aria-hidden="true">L</span>
            LinuxDo 登录
          </a>
        </div>

        <div className="auth-divider"><span>或使用邮箱</span></div>

        <form className="auth-form" onSubmit={handleSubmit}>
          {isRegister && (
            <label>
              <span>名字</span>
              <input
                name="name"
                autoComplete="name"
                placeholder="名字"
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
              />
            </label>
          )}

          <label>
            <span>邮箱</span>
            <input
              name="email"
              type="email"
              autoComplete="email"
              placeholder="邮箱"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </label>

          <label>
            <span>密码</span>
            <input
              name="password"
              type="password"
              autoComplete={isRegister ? 'new-password' : 'current-password'}
              placeholder="密码"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>

          {error && <div className="auth-error" role="alert">{error}</div>}

          <button className="auth-submit" type="submit" disabled={isSubmitting}>
            {isSubmitting ? '正在连接…' : isRegister ? '注册并进入城市' : '进入城市'}
          </button>
        </form>

        <p className="auth-panel__note">首次进入后将继续完成居民创建与世界引导。</p>
      </section>
    </main>
  )
}
