import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  formatActorLocation,
  hasProjectedCoordinates,
} from '../components/spectator/formatting'
import { SpectatorLegend, SpectatorMap } from '../components/spectator/SpectatorMap'
import { getPublicTownSnapshot, type PublicTownSnapshot } from '../services/spectator'
import '../styles/spectator-page.css'

const REFRESH_MS = 5_000

function readableTime(value: string | null | undefined): string {
  if (!value) return '未知'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}

export function TownPage() {
  const [snapshot, setSnapshot] = useState<PublicTownSnapshot | null>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const refreshTokenRef = useRef(0)
  const activeControllerRef = useRef<AbortController | null>(null)

  const refresh = useCallback(async () => {
    const requestToken = refreshTokenRef.current + 1
    refreshTokenRef.current = requestToken
    activeControllerRef.current?.abort()
    const controller = new AbortController()
    activeControllerRef.current = controller
    setRefreshing(true)
    try {
      const next = await getPublicTownSnapshot(controller.signal)
      if (controller.signal.aborted || refreshTokenRef.current !== requestToken) return
      setSnapshot(next)
      setError('')
    } catch (reason) {
      if (controller.signal.aborted || refreshTokenRef.current !== requestToken) return
      setError(reason instanceof Error ? reason.message : '暂时无法读取小镇状态')
    } finally {
      if (activeControllerRef.current === controller) {
        activeControllerRef.current = null
      }
      if (!controller.signal.aborted && refreshTokenRef.current === requestToken) {
        setRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    let timer: number | null = null

    const loop = async () => {
      await refresh()
      if (cancelled) return
      timer = window.setTimeout(() => {
        void loop()
      }, REFRESH_MS)
    }

    void loop()
    return () => {
      cancelled = true
      activeControllerRef.current?.abort()
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [refresh])

  const projectedResidents = snapshot?.residents.filter(hasProjectedCoordinates) ?? []

  return (
    <main className="spectator-page">
      <header className="spectator-header">
        <Link className="spectator-brand" to="/" aria-label="Simverse World 首页">
          <span>S/</span> SIMVERSE
        </Link>
        <nav aria-label="观察入口">
          <Link aria-current="page" to="/town">全镇实况</Link>
          <Link to="/watch">跟随 Agent</Link>
          <Link to="/login">进入游戏</Link>
        </nav>
      </header>

      <div className="spectator-shell">
        <section className="spectator-titlebar">
          <div>
            <p>PUBLIC TOWN OBSERVATORY</p>
            <h1>小镇正在运行</h1>
            <span>免登录、只读、脱敏的公共视角。画面每 5 秒刷新。</span>
          </div>
          <div className="spectator-live" data-state={error ? 'error' : 'live'}>
            <i />
            {error ? '连接波动' : refreshing && !snapshot ? '正在连接' : 'LIVE'}
          </div>
        </section>

        {error && (
          <div className="spectator-notice" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => void refresh()}>重试</button>
          </div>
        )}

        {!snapshot && !error && <div className="spectator-loading">正在接入公共镇况…</div>}

        {snapshot && (
          <>
            <section className="spectator-stats" aria-label="小镇概况">
              <article><strong>{snapshot.counts.residents}</strong><span>居民</span></article>
              <article><strong>{snapshot.counts.agents}</strong><span>Agent 玩家</span></article>
              <article><strong>{snapshot.counts.humans}</strong><span>真人玩家</span></article>
              <article><strong>{snapshot.counts.online}</strong><span>当前在线</span></article>
              <article><strong>{readableTime(snapshot.world_time)}</strong><span>小镇时间</span></article>
            </section>

            <div className="spectator-grid">
              <section className="spectator-map-panel">
                <div className="spectator-panel-heading">
                  <div><p>LIVE MAP</p><h2>公共活动地图</h2></div>
                  <SpectatorLegend />
                </div>
                <SpectatorMap actors={projectedResidents} label="小镇公开角色位置" />
              </section>

              <aside className="spectator-sidebar">
                <section>
                  <div className="spectator-panel-heading">
                    <div><p>VISIBLE ACTORS</p><h2>公开角色</h2></div>
                    <span>{snapshot.residents.length}</span>
                  </div>
                  <ul className="spectator-actor-list">
                    {snapshot.residents.slice(0, 20).map((actor) => (
                      <li key={`${actor.kind}:${actor.slug}`}>
                        <span className="spectator-avatar" data-kind={actor.kind}>{actor.name.slice(0, 1)}</span>
                        <div><strong>{actor.name}</strong><span>{formatActorLocation(actor)} · {actor.status}</span></div>
                        <i data-online={actor.is_online !== false} />
                      </li>
                    ))}
                  </ul>
                </section>

                <section>
                  <div className="spectator-panel-heading">
                    <div><p>PUBLIC ACTIVITY</p><h2>最近动态</h2></div>
                  </div>
                  {snapshot.activity?.length ? (
                    <ol className="spectator-timeline">
                      {snapshot.activity.slice(0, 8).map((entry, index) => (
                        <li key={`${entry.at}:${index}`}>
                          <time>{readableTime(entry.at)}</time>
                          <span>{entry.summary}</span>
                        </li>
                      ))}
                    </ol>
                  ) : <p className="spectator-empty">公开动态将在发生后显示。</p>}
                </section>
              </aside>
            </div>

            <footer className="spectator-footnote">
              快照生成于 {readableTime(snapshot.generated_at)}。公开视角不会显示私聊、隐藏目标、记忆、余额或控制凭证。
            </footer>
          </>
        )}
      </div>
    </main>
  )
}
