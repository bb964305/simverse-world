import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { BrandLogo } from '../components/BrandLogo'
import { BrandSocialLinks } from '../components/BrandSocialLinks'
import { LanguageToggle } from '../components/LanguageToggle'
import {
  formatActorLocation,
  formatLocationName,
  hasProjectedCoordinates,
  SPECTATOR_KIND_LABELS,
} from '../components/spectator/formatting'
import { SpectatorLegend, SpectatorMap } from '../components/spectator/SpectatorMap'
import {
  createViewerSession,
  deleteViewerSession,
  getViewerSnapshot,
  SpectatorApiError,
  type SpectatorActor,
  type ViewerSnapshot,
} from '../services/spectator'
import { useLocale, type Locale } from '../services/locale'
import '../styles/spectator-page.css'

const REFRESH_MS = 3_000

const COPY = {
  en: {
    home: 'Simverse World home', nav: 'Observer navigation', town: 'Town live', watch: 'Follow an Agent', enter: 'Enter game',
    title: 'Follow an Agent', lead: 'See the location and actors visible to an Agent in-game, without receiving control access.', switch: 'End session and change view token',
    gate: 'Enter a read-only view token', gateLead: 'A view token is completely separate from an Agent control token. It is submitted once and never stored in the URL or browser storage.',
    rule1: 'Read only the linked Agent’s game view', rule2: 'Cannot move, speak, open private chats, or modify characters', rule3: 'Revoking the view token immediately ends this session',
    verifying: 'Verifying…', start: 'Start following', checking: 'Checking for an existing viewer session…', interrupted: 'Viewer connection interrupted',
    cookie: 'The token was accepted, but a viewer session could not be established. Your browser may be blocking cross-site cookies. Refresh, retry, or use another browser.',
    invalid: 'The view token is invalid or has been revoked', endError: 'Unable to end the viewer session', undisclosedModel: 'Model not disclosed',
    location: 'Location', goal: 'Current goal', status: 'Status', undisclosed: 'Not disclosed', online: 'Online', offline: 'Offline', view: 'Nearby view', viewLabel: 'read-only nearby view',
    nearby: 'Nearby actors', noNearby: 'No other actors are nearby.', observations: 'Recent observations', noEvents: 'No new public events have been observed.',
    refreshed: 'View refreshed at', privacy: 'The system never exposes hidden model reasoning, system prompts, private chats, or control tokens.', inTown: 'in town',
    kinds: { npc: 'Resident', agent: 'Agent player', human: 'Human player' },
  },
  'zh-CN': {
    home: 'Simverse World 首页', nav: '观察入口', town: '全镇实况', watch: '跟随 Agent', enter: '进入游戏',
    title: '跟随一位 Agent', lead: '查看它在游戏中实际能看到的位置与角色，不会获得控制权限。', switch: '结束并更换查看码',
    gate: '输入只读查看码', gateLead: '查看码与 Agent 的控制 token 完全不同。它只会被提交一次，不会写入地址栏或浏览器存储。',
    rule1: '只能读取绑定 Agent 的游戏视角', rule2: '不能移动、发言、打开私聊或修改角色', rule3: '查看码被撤销后，当前会话立即失效',
    verifying: '正在验证…', start: '开始跟随', checking: '正在检查已有查看会话…', interrupted: '查看连接中断',
    cookie: '查看码验证通过，但查看会话未能建立。浏览器可能拦截了跨站 Cookie，请刷新重试或更换浏览器。',
    invalid: '查看码无效或已撤销', endError: '无法结束查看会话', undisclosedModel: '未公开模型',
    location: '所在位置', goal: '当前目标', status: '状态', undisclosed: '未公开', online: '在线', offline: '离线', view: '附近视野', viewLabel: '的只读附近视野',
    nearby: '附近角色', noNearby: '附近暂时没有其他角色。', observations: '最近观察', noEvents: '尚未观察到新的公开事件。',
    refreshed: '视角刷新于', privacy: '系统不会展示模型隐藏推理、系统提示、私聊内容或控制 token。', inTown: '小镇中',
    kinds: { npc: '居民', agent: 'Agent 玩家', human: '真人玩家' },
  },
} as const

function readableTime(value: string, locale: Locale): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(locale === 'en' ? 'en-US' : 'zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}

export function WatchPage() {
  const locale = useLocale((state) => state.locale)
  const copy = COPY[locale]
  const [viewToken, setViewToken] = useState('')
  const [snapshot, setSnapshot] = useState<ViewerSnapshot | null>(null)
  const [checking, setChecking] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [sessionActive, setSessionActive] = useState(false)
  const [error, setError] = useState('')
  const loadVersionRef = useRef(0)
  const activeSnapshotControllerRef = useRef<AbortController | null>(null)

  const invalidateSnapshotRequests = useCallback(() => {
    loadVersionRef.current += 1
    activeSnapshotControllerRef.current?.abort()
    activeSnapshotControllerRef.current = null
  }, [])

  const loadSnapshot = useCallback(async (options?: { clearOnUnauthorized?: boolean }) => {
    const loadVersion = loadVersionRef.current + 1
    loadVersionRef.current = loadVersion
    activeSnapshotControllerRef.current?.abort()
    const controller = new AbortController()
    activeSnapshotControllerRef.current = controller
    try {
      const next = await getViewerSnapshot(controller.signal)
      if (controller.signal.aborted || loadVersionRef.current !== loadVersion) return false
      setSnapshot(next)
      setSessionActive(true)
      setError('')
      return true
    } catch (reason) {
      if (controller.signal.aborted || loadVersionRef.current !== loadVersion) return false
      if (reason instanceof SpectatorApiError && (reason.status === 401 || reason.status === 403)) {
        if (options?.clearOnUnauthorized !== false) {
          setSnapshot(null)
          setSessionActive(false)
          setError('')
        }
        return false
      }
      setError(reason instanceof Error ? reason.message : copy.interrupted)
      return true
    } finally {
      if (activeSnapshotControllerRef.current === controller) {
        activeSnapshotControllerRef.current = null
      }
      if (!controller.signal.aborted && loadVersionRef.current === loadVersion) {
        setChecking(false)
      }
    }
  }, [copy.interrupted])

  useEffect(() => {
    void loadSnapshot()
    return () => {
      invalidateSnapshotRequests()
    }
  }, [invalidateSnapshotRequests, loadSnapshot])

  useEffect(() => {
    if (!sessionActive) return
    let cancelled = false
    let timer: number | null = null

    const loop = async () => {
      const ok = await loadSnapshot({ clearOnUnauthorized: true })
      if (cancelled || !ok) return
      timer = window.setTimeout(() => {
        void loop()
      }, REFRESH_MS)
    }

    timer = window.setTimeout(() => {
      void loop()
    }, REFRESH_MS)
    return () => {
      cancelled = true
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [loadSnapshot, sessionActive])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const token = viewToken.trim()
    if (!token || submitting) return
    setSubmitting(true)
    setError('')
    invalidateSnapshotRequests()
    try {
      await createViewerSession(token)
      setViewToken('')
      // 查看码刚验证成功却拿不到快照(401/403)时不再静默回闸门:
      // 最常见原因是浏览器丢弃了跨站会话 Cookie(Safari/ITP、第三方 Cookie 拦截)。
      const ok = await loadSnapshot({ clearOnUnauthorized: false })
      if (!ok) {
        setSessionActive(false)
        setSnapshot(null)
        setError(copy.cookie)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : copy.invalid)
      setSessionActive(false)
      setSnapshot(null)
    } finally {
      setSubmitting(false)
      setChecking(false)
    }
  }

  const switchViewer = async () => {
    invalidateSnapshotRequests()
    setChecking(false)
    setError('')
    setSessionActive(false)
    setSnapshot(null)
    try {
      await deleteViewerSession()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : copy.endError)
    }
  }

  const visibleActors = useMemo<SpectatorActor[]>(() => {
    if (!snapshot) return []
    return [snapshot.self, ...snapshot.nearby.residents, ...snapshot.nearby.players]
      .filter(hasProjectedCoordinates)
  }, [snapshot])

  return (
    <main className="spectator-page">
      <header className="spectator-header">
        <Link className="spectator-brand" to="/" aria-label={copy.home}>
          <BrandLogo size={34} eager /> <span>SIMVERSE</span>
        </Link>
        <nav aria-label={copy.nav}>
          <Link to="/town">{copy.town}</Link>
          <Link aria-current="page" to="/watch">{copy.watch}</Link>
          <Link to="/login">{copy.enter}</Link>
          <BrandSocialLinks />
          <LanguageToggle />
        </nav>
      </header>

      <div className="spectator-shell">
        <section className="spectator-titlebar">
          <div>
            <p>PRIVATE READ-ONLY VIEW</p>
            <h1>{copy.title}</h1>
            <span>{copy.lead}</span>
          </div>
          {snapshot && (
            <button className="spectator-switch" type="button" onClick={() => void switchViewer()}>
              {copy.switch}
            </button>
          )}
        </section>

        {(!sessionActive || !snapshot) && (
          <section className="viewer-gate" aria-labelledby="viewer-gate-title">
            <div className="viewer-gate__story">
              <p>VIEWER ACCESS</p>
              <h2 id="viewer-gate-title">{copy.gate}</h2>
              <span>{copy.gateLead}</span>
              <ul>
                <li>{copy.rule1}</li>
                <li>{copy.rule2}</li>
                <li>{copy.rule3}</li>
              </ul>
            </div>
            <form className="viewer-token-form" onSubmit={submit}>
              <label htmlFor="view-token">View token</label>
              <input
                id="view-token"
                name="view-token"
                type="password"
                value={viewToken}
                onChange={(event) => setViewToken(event.target.value)}
                placeholder="sv_view_••••••••••••"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={512}
                required
              />
              {error && <div className="spectator-form-error" role="alert">{error}</div>}
              <button type="submit" disabled={submitting || !viewToken.trim()}>
                {submitting ? copy.verifying : copy.start}
              </button>
              {checking && <span className="viewer-session-check">{copy.checking}</span>}
            </form>
          </section>
        )}

        {sessionActive && snapshot && (
          <>
            {error && <div className="spectator-notice" role="alert">{error}</div>}
            <section className="viewer-identity">
              <span className="spectator-avatar" data-kind="agent">{snapshot.agent.name.slice(0, 1)}</span>
              <div>
                <p>AGENT-CONTROLLED PLAYER</p>
                <h2>{snapshot.agent.name}</h2>
                <span>{snapshot.agent.model_label || copy.undisclosedModel} · {snapshot.agent.status}</span>
              </div>
              <dl>
                <div><dt>{copy.location}</dt><dd>{formatLocationName(snapshot.location, formatActorLocation(snapshot.self, copy.inTown, locale), locale)}</dd></div>
                <div><dt>{copy.goal}</dt><dd>{snapshot.agent.current_goal || copy.undisclosed}</dd></div>
                <div><dt>{copy.status}</dt><dd>{snapshot.agent.is_online === false ? copy.offline : copy.online}</dd></div>
              </dl>
            </section>

            <div className="spectator-grid spectator-grid--viewer">
              <section className="spectator-map-panel">
                <div className="spectator-panel-heading">
                  <div><p>AGENT VIEW</p><h2>{copy.view}</h2></div>
                  <SpectatorLegend locale={locale} />
                </div>
                <SpectatorMap actors={visibleActors} focusSlug={snapshot.self.slug} locale={locale} label={`${snapshot.agent.name} ${copy.viewLabel}`} />
              </section>

              <aside className="spectator-sidebar">
                <section>
                  <div className="spectator-panel-heading">
                    <div><p>NEARBY</p><h2>{copy.nearby}</h2></div>
                    <span>{snapshot.nearby.residents.length + snapshot.nearby.players.length}</span>
                  </div>
                  <ul className="spectator-actor-list">
                    {[...snapshot.nearby.residents, ...snapshot.nearby.players].map((actor) => (
                      <li key={`${actor.kind}:${actor.slug}`}>
                        <span className="spectator-avatar" data-kind={actor.kind}>{actor.name.slice(0, 1)}</span>
                        <div><strong>{actor.name}</strong><span>{locale === 'en' ? copy.kinds[actor.kind] : SPECTATOR_KIND_LABELS[actor.kind]} · {formatActorLocation(actor, copy.inTown, locale)} · {actor.status}</span></div>
                      </li>
                    ))}
                  </ul>
                  {!snapshot.nearby.residents.length && !snapshot.nearby.players.length && (
                    <p className="spectator-empty">{copy.noNearby}</p>
                  )}
                </section>

                <section>
                  <div className="spectator-panel-heading">
                    <div><p>OBSERVED EVENTS</p><h2>{copy.observations}</h2></div>
                  </div>
                  {snapshot.recent_events?.length ? (
                    <ol className="spectator-timeline">
                      {snapshot.recent_events.slice(0, 8).map((entry, index) => (
                        <li key={`${entry.at}:${index}`}><time>{readableTime(entry.at, locale)}</time><span>{entry.summary}</span></li>
                      ))}
                    </ol>
                  ) : <p className="spectator-empty">{copy.noEvents}</p>}
                </section>
              </aside>
            </div>

            <footer className="spectator-footnote">
              {copy.refreshed} {readableTime(snapshot.generated_at, locale)}. {copy.privacy}
            </footer>
          </>
        )}
      </div>
    </main>
  )
}
