import { useEffect, useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { useGameStore } from '../stores/gameStore'
import {
  DEMOS,
  FAQ,
  FEATURES,
  PROOF,
  SHOWCASES,
  STACK,
  STATS,
  STEPS,
} from './landing/content'
import './LandingPage.css'

function FeatureIcon({ tone }: { tone: 'cyan' | 'mag' }) {
  const paths =
    tone === 'cyan' ? (
      <path d="M12 12a4 4 0 1 0-4-4 4 4 0 0 0 4 4ZM4 20a8 8 0 0 1 16 0" />
    ) : (
      <>
        <path d="M4 7h16M4 12h10M4 17h14" />
        <circle cx="18" cy="12" r="2" />
      </>
    )
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      {paths}
    </svg>
  )
}

export function LandingPage() {
  const token = useGameStore((s) => s.token)
  const enterTo = token ? '/play' : '/login?next=/play'
  const [menuOpen, setMenuOpen] = useState(false)
  const menuId = useId()

  useEffect(() => {
    if (!menuOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menuOpen])

  useEffect(() => {
    const nodes = document.querySelectorAll('.mkt-reveal')
    if (!nodes.length || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      nodes.forEach((n) => n.classList.add('is-visible'))
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible')
            io.unobserve(entry.target)
          }
        }
      },
      { threshold: 0.12, rootMargin: '0px 0px -8% 0px' },
    )
    nodes.forEach((n) => io.observe(n))
    return () => io.disconnect()
  }, [])

  const closeMenu = () => setMenuOpen(false)

  return (
    <div className="mkt">
      <a className="mkt-skip" href="#main">
        跳到主内容
      </a>
      <div className="mkt-atmosphere" aria-hidden="true" />
      <div className="mkt-grid" aria-hidden="true" />

      <div className="mkt-shell">
        <header className="mkt-nav">
          <Link to="/" className="mkt-brand" aria-label="Simverse World 首页">
            <span className="mkt-brand-mark" aria-hidden="true" />
            <span>Simverse World</span>
          </Link>

          <nav className="mkt-nav-links" aria-label="页面导航">
            <a href="#features">能力</a>
            <a href="#demo">演示</a>
            <a href="#showcase">世界</a>
            <a href="#faq">FAQ</a>
          </nav>

          <div className="mkt-nav-actions">
            {!token && (
              <Link to="/login?next=/play" className="mkt-btn mkt-btn-ghost mkt-btn-sm mkt-hide-sm">
                登录
              </Link>
            )}
            <Link to={enterTo} className="mkt-btn mkt-btn-primary mkt-btn-sm">
              {token ? '进入世界' : '立即体验'}
            </Link>
            <button
              type="button"
              className="mkt-menu-btn"
              aria-expanded={menuOpen}
              aria-controls={menuId}
              aria-label={menuOpen ? '关闭菜单' : '打开菜单'}
              onClick={() => setMenuOpen((v) => !v)}
            >
              <span />
              <span />
              <span />
            </button>
          </div>
        </header>

        {menuOpen && (
          <div
            id={menuId}
            className="mkt-mobile-menu"
            role="dialog"
            aria-modal="true"
            aria-label="移动导航"
          >
            <a href="#features" onClick={closeMenu}>能力</a>
            <a href="#demo" onClick={closeMenu}>演示</a>
            <a href="#showcase" onClick={closeMenu}>世界</a>
            <a href="#how" onClick={closeMenu}>流程</a>
            <a href="#faq" onClick={closeMenu}>FAQ</a>
            <a href="#stack" onClick={closeMenu}>技术</a>
            {!token && (
              <Link to="/login?next=/play" onClick={closeMenu} className="mkt-btn mkt-btn-ghost">
                登录
              </Link>
            )}
            <Link to={enterTo} onClick={closeMenu} className="mkt-btn mkt-btn-primary">
              {token ? '进入世界' : '免费进入城市'}
            </Link>
          </div>
        )}

        <main id="main">
          <section className="mkt-main mkt-hero">
            <div className="mkt-hero-grid">
              <div className="mkt-hero-copy">
                <div className="mkt-eyebrow">
                  <span className="mkt-live-dot" aria-hidden="true" />
                  永不关闭的赛博城市
                </div>
                <h1>赛博永生开放世界</h1>
                <p className="mkt-hero-lead">
                  有记忆、有人格、会过日子的 AI 居民，住在一座像素城市里。
                  不是聊天窗——是你可以走进去的世界。
                </p>
                <div className="mkt-hero-cta">
                  <Link to={enterTo} className="mkt-btn mkt-btn-primary">
                    {token ? '回到世界' : '免费进入城市'}
                  </Link>
                  <a href="#demo" className="mkt-btn mkt-btn-ghost">
                    观看演示
                  </a>
                </div>
                <div className="mkt-proof" aria-label="可信背书">
                  {PROOF.map((p) => (
                    <span key={p} className="mkt-proof-item">{p}</span>
                  ))}
                </div>
              </div>

              <figure className="mkt-hero-frame mkt-frame">
                <img
                  src="/marketing/game-overview.webp"
                  alt="Simverse World 游戏主界面"
                  width={1280}
                  height={710}
                  fetchPriority="high"
                />
                <figcaption>实时开放世界</figcaption>
                <div className="mkt-skyline" aria-hidden="true" />
              </figure>
            </div>

            <div className="mkt-stats" aria-label="产品亮点数据">
              {STATS.map((s) => (
                <div key={s.label} className="mkt-stat">
                  <strong>{s.value}</strong>
                  <span>{s.label}</span>
                </div>
              ))}
            </div>
          </section>

          <section id="features" className="mkt-main mkt-section mkt-reveal">
            <div className="mkt-section-head">
              <span className="mkt-kicker">Capabilities</span>
              <h2>不是聊天机器人，是会过日子的居民</h2>
              <p>从记忆、人格到作息与社交，Simverse 把生成式智能体放进可探索的开放世界。</p>
            </div>
            <div className="mkt-features">
              {FEATURES.map((f) => (
                <article key={f.title} className="mkt-card">
                  <div className={`mkt-icon${f.tone === 'mag' ? ' mkt-icon-mag' : ''}`}>
                    <FeatureIcon tone={f.tone} />
                  </div>
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </article>
              ))}
            </div>
          </section>

          <section id="demo" className="mkt-main mkt-section mkt-reveal">
            <div className="mkt-section-head">
              <span className="mkt-kicker">Live Demo</span>
              <h2>真实录屏，非概念片</h2>
              <p>直接看对话与传送——这是正在运转的城市，不是静态 mock。</p>
            </div>
            <div className="mkt-demos">
              {DEMOS.map((d) => (
                <article key={d.src} className="mkt-demo">
                  <div className="mkt-frame mkt-video-frame">
                    <video
                      controls
                      preload="none"
                      poster={d.poster}
                      playsInline
                    >
                      <source src={d.src} type="video/webm" />
                    </video>
                  </div>
                  <h3>{d.title}</h3>
                  <p>{d.body}</p>
                </article>
              ))}
            </div>
          </section>

          <section id="showcase" className="mkt-main mkt-section mkt-reveal">
            <div className="mkt-section-head">
              <span className="mkt-kicker">World Preview</span>
              <h2>一眼看见这座城市</h2>
              <p>像素世界、工坊区与角色锻造管线，构成从游玩到创造的完整循环。</p>
            </div>
            <div className="mkt-showcase">
              {SHOWCASES.map((s) => (
                <figure key={s.src} className="mkt-frame">
                  <img src={s.src} alt={s.alt} loading="lazy" width={1280} height={710} />
                  <figcaption>{s.caption}</figcaption>
                </figure>
              ))}
            </div>
          </section>

          <section id="how" className="mkt-main mkt-section mkt-reveal">
            <div className="mkt-section-head">
              <span className="mkt-kicker">How it works</span>
              <h2>三步走进赛博永生</h2>
              <p>从登录到锻造，路径足够短，深度留给你在城里慢慢发现。</p>
            </div>
            <div className="mkt-steps">
              {STEPS.map((s) => (
                <article key={s.title} className="mkt-step">
                  <h3>{s.title}</h3>
                  <p>{s.body}</p>
                </article>
              ))}
            </div>
          </section>

          <section id="faq" className="mkt-main mkt-section mkt-reveal">
            <div className="mkt-section-head">
              <span className="mkt-kicker">FAQ</span>
              <h2>进门前常见问题</h2>
              <p>先把顾虑说清楚，再走进城门。</p>
            </div>
            <div className="mkt-faq">
              {FAQ.map((item) => (
                <details key={item.q} className="mkt-faq-item">
                  <summary>{item.q}</summary>
                  <p>{item.a}</p>
                </details>
              ))}
            </div>
          </section>

          <section id="stack" className="mkt-main mkt-section mkt-reveal">
            <div className="mkt-section-head">
              <span className="mkt-kicker">Stack</span>
              <h2>为「活世界」准备的技术底座</h2>
              <p>异步后端、向量记忆、实时同步与 2D 引擎协同，支撑居民永不下线的日常。</p>
            </div>
            <div className="mkt-chips">
              {STACK.map((item) => (
                <span key={item} className="mkt-chip">
                  {item}
                </span>
              ))}
            </div>
          </section>

          <section className="mkt-main mkt-reveal">
            <div className="mkt-cta-band">
              <h2>今晚就去城里走走</h2>
              <p>一座永不关闭的赛博城市，正在等你成为其中一位居民。</p>
              <Link to={enterTo} className="mkt-btn mkt-btn-primary">
                {token ? '进入世界' : '免费进入城市'}
              </Link>
            </div>
          </section>
        </main>

        <footer className="mkt-footer">
          <div className="mkt-footer-inner">
            <div>© {new Date().getFullYear()} Simverse World · MIT</div>
            <div className="mkt-footer-links">
              <a href="https://simverse.world" target="_blank" rel="noreferrer">
                线上世界
              </a>
              <a href="https://linux.do/" target="_blank" rel="noreferrer">
                LinuxDo
              </a>
              <Link to="/login?next=/play">登录</Link>
            </div>
          </div>
        </footer>
      </div>

    </div>
  )
}
