import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'
import { BrandSocialLinks } from '../components/BrandSocialLinks'
import { LanguageToggle } from '../components/LanguageToggle'
import {
  formatActorLocation,
  hasProjectedCoordinates,
} from '../components/spectator/formatting'
import { SpectatorLegend, SpectatorMap } from '../components/spectator/SpectatorMap'
import { getPublicTownSnapshot, type PublicTownSnapshot } from '../services/spectator'
import { useLocale, type Locale } from '../services/locale'
import '../styles/spectator-page.css'

const REFRESH_MS = 5_000

const COPY = {
  en: {
    home: 'Simverse World home', nav: 'Observer navigation', town: 'Town live', watch: 'Follow an Agent', enter: 'Enter game',
    title: 'The town is alive.', lead: 'Anonymous, read-only, privacy-safe public view. Refreshed every 5 seconds.', unstable: 'Connection unstable', connecting: 'Connecting',
    retry: 'Retry', loading: 'Connecting to the public town feed…', unknown: 'Unknown', error: 'The public town feed is temporarily unavailable.', overview: 'Town overview',
    residents: 'Residents', agents: 'Agent players', humans: 'Human players', online: 'Online now', time: 'Town time', map: 'Public activity map', mapLabel: 'Public actor positions in town',
    visible: 'Visible actors', activity: 'Recent activity', empty: 'Public activity appears here as it happens.', inTown: 'In town',
    footA: 'Snapshot generated at', footB: 'The public view never exposes private chats, hidden goals, memories, balances, or control credentials.',
  },
  'zh-CN': {
    home: 'Simverse World 首页', nav: '观察入口', town: '全镇实况', watch: '跟随 Agent', enter: '进入游戏',
    title: '小镇正在运行', lead: '免登录、只读、脱敏的公共视角。画面每 5 秒刷新。', unstable: '连接波动', connecting: '正在连接',
    retry: '重试', loading: '正在接入公共镇况…', unknown: '未知', error: '暂时无法读取小镇状态', overview: '小镇概况',
    residents: '居民', agents: 'Agent 玩家', humans: '真人玩家', online: '当前在线', time: '小镇时间', map: '公共活动地图', mapLabel: '小镇公开角色位置',
    visible: '公开角色', activity: '最近动态', empty: '公开动态将在发生后显示。', inTown: '小镇中',
    footA: '快照生成于', footB: '公开视角不会显示私聊、隐藏目标、记忆、余额或控制凭证。',
  },
} as const

function readableTime(value: string | null | undefined, locale: Locale, unknown: string): string {
  if (!value) return unknown
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(locale === 'en' ? 'en-US' : 'zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}

export function TownPage() {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
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
      setError(reason instanceof Error ? reason.message : copy.error)
    } finally {
      if (activeControllerRef.current === controller) {
        activeControllerRef.current = null
      }
      if (!controller.signal.aborted && refreshTokenRef.current === requestToken) {
        setRefreshing(false)
      }
    }
  }, [copy.error])

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
        <Link className="spectator-brand" to="/" aria-label={copy.home}>
          <BrandLogo size={34} eager /> <span>SIMVERSE</span>
        </Link>
        <nav aria-label={copy.nav}>
          <Link aria-current="page" to="/town">{copy.town}</Link>
          <Link to="/watch">{copy.watch}</Link>
          <Link to="/login">{copy.enter}</Link>
          <BrandSocialLinks />
          <LanguageToggle />
        </nav>
      </header>

      <div className="spectator-shell">
        <section className="spectator-titlebar">
          <div>
            <p>PUBLIC TOWN OBSERVATORY</p>
            <h1>{copy.title}</h1>
            <span>{copy.lead}</span>
          </div>
          <div className="spectator-live" data-state={error ? 'error' : 'live'}>
            <i />
            {error ? copy.unstable : refreshing && !snapshot ? copy.connecting : 'LIVE'}
          </div>
        </section>

        {error && (
          <div className="spectator-notice" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => void refresh()}>{copy.retry}</button>
          </div>
        )}

        {!snapshot && !error && <div className="spectator-loading">{copy.loading}</div>}

        {snapshot && (
          <>
            <section className="spectator-stats" aria-label={copy.overview}>
              <article><strong>{snapshot.counts.residents}</strong><span>{copy.residents}</span></article>
              <article><strong>{snapshot.counts.agents}</strong><span>{copy.agents}</span></article>
              <article><strong>{snapshot.counts.humans}</strong><span>{copy.humans}</span></article>
              <article><strong>{snapshot.counts.online}</strong><span>{copy.online}</span></article>
              <article><strong>{readableTime(snapshot.world_time, locale, copy.unknown)}</strong><span>{copy.time}</span></article>
            </section>

            <div className="spectator-grid">
              <section className="spectator-map-panel">
                <div className="spectator-panel-heading">
                  <div><p>LIVE MAP</p><h2>{copy.map}</h2></div>
                  <SpectatorLegend locale={locale} />
                </div>
                <SpectatorMap actors={projectedResidents} label={copy.mapLabel} locale={locale} />
              </section>

              <aside className="spectator-sidebar">
                <section>
                  <div className="spectator-panel-heading">
                    <div><p>VISIBLE ACTORS</p><h2>{copy.visible}</h2></div>
                    <span>{snapshot.residents.length}</span>
                  </div>
                  <ul className="spectator-actor-list">
                    {snapshot.residents.slice(0, 20).map((actor) => (
                      <li key={`${actor.kind}:${actor.slug}`}>
                        <span className="spectator-avatar" data-kind={actor.kind}>{actor.name.slice(0, 1)}</span>
                        <div><strong>{actor.name}</strong><span>{formatActorLocation(actor, copy.inTown, locale)} · {actor.status}</span></div>
                        <i data-online={actor.is_online !== false} />
                      </li>
                    ))}
                  </ul>
                </section>

                <section>
                  <div className="spectator-panel-heading">
                    <div><p>PUBLIC ACTIVITY</p><h2>{copy.activity}</h2></div>
                  </div>
                  {snapshot.activity?.length ? (
                    <ol className="spectator-timeline">
                      {snapshot.activity.slice(0, 8).map((entry, index) => (
                        <li key={`${entry.at}:${index}`}>
                          <time>{readableTime(entry.at, locale, copy.unknown)}</time>
                          <span>{entry.summary}</span>
                        </li>
                      ))}
                    </ol>
                  ) : <p className="spectator-empty">{copy.empty}</p>}
                </section>
              </aside>
            </div>

            <footer className="spectator-footnote">
              {copy.footA} {readableTime(snapshot.generated_at, locale, copy.unknown)}. {copy.footB}
            </footer>
          </>
        )}
      </div>
    </main>
  )
}
