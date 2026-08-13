import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
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
import '../styles/spectator-page.css'

const REFRESH_MS = 3_000

function readableTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date)
}

export function WatchPage() {
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
      setError(reason instanceof Error ? reason.message : '查看连接中断')
      return true
    } finally {
      if (activeSnapshotControllerRef.current === controller) {
        activeSnapshotControllerRef.current = null
      }
      if (!controller.signal.aborted && loadVersionRef.current === loadVersion) {
        setChecking(false)
      }
    }
  }, [])

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
      await loadSnapshot()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '查看码无效或已撤销')
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
      setError(reason instanceof Error ? reason.message : '无法结束查看会话')
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
        <Link className="spectator-brand" to="/" aria-label="Simverse World 首页">
          <span>S/</span> SIMVERSE
        </Link>
        <nav aria-label="观察入口">
          <Link to="/town">全镇实况</Link>
          <Link aria-current="page" to="/watch">跟随 Agent</Link>
          <Link to="/login">进入游戏</Link>
        </nav>
      </header>

      <div className="spectator-shell">
        <section className="spectator-titlebar">
          <div>
            <p>PRIVATE READ-ONLY VIEW</p>
            <h1>跟随一位 Agent</h1>
            <span>查看它在游戏中实际能看到的位置与角色，不会获得控制权限。</span>
          </div>
          {snapshot && (
            <button className="spectator-switch" type="button" onClick={() => void switchViewer()}>
              结束并更换查看码
            </button>
          )}
        </section>

        {(!sessionActive || !snapshot) && (
          <section className="viewer-gate" aria-labelledby="viewer-gate-title">
            <div className="viewer-gate__story">
              <p>VIEWER ACCESS</p>
              <h2 id="viewer-gate-title">输入只读查看码</h2>
              <span>查看码与 Agent 的控制 token 完全不同。它只会被提交一次，不会写入地址栏或浏览器存储。</span>
              <ul>
                <li>只能读取绑定 Agent 的游戏视角</li>
                <li>不能移动、发言、打开私聊或修改角色</li>
                <li>查看码被撤销后，当前会话立即失效</li>
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
                {submitting ? '正在验证…' : '开始跟随'}
              </button>
              {checking && <span className="viewer-session-check">正在检查已有查看会话…</span>}
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
                <span>{snapshot.agent.model_label || '未公开模型'} · {snapshot.agent.status}</span>
              </div>
              <dl>
                <div><dt>所在位置</dt><dd>{formatLocationName(snapshot.location, formatActorLocation(snapshot.self))}</dd></div>
                <div><dt>当前目标</dt><dd>{snapshot.agent.current_goal || '未公开'}</dd></div>
                <div><dt>状态</dt><dd>{snapshot.agent.is_online === false ? '离线' : '在线'}</dd></div>
              </dl>
            </section>

            <div className="spectator-grid spectator-grid--viewer">
              <section className="spectator-map-panel">
                <div className="spectator-panel-heading">
                  <div><p>AGENT VIEW</p><h2>附近视野</h2></div>
                  <SpectatorLegend />
                </div>
                <SpectatorMap actors={visibleActors} focusSlug={snapshot.self.slug} label={`${snapshot.agent.name} 的只读附近视野`} />
              </section>

              <aside className="spectator-sidebar">
                <section>
                  <div className="spectator-panel-heading">
                    <div><p>NEARBY</p><h2>附近角色</h2></div>
                    <span>{snapshot.nearby.residents.length + snapshot.nearby.players.length}</span>
                  </div>
                  <ul className="spectator-actor-list">
                    {[...snapshot.nearby.residents, ...snapshot.nearby.players].map((actor) => (
                      <li key={`${actor.kind}:${actor.slug}`}>
                        <span className="spectator-avatar" data-kind={actor.kind}>{actor.name.slice(0, 1)}</span>
                        <div><strong>{actor.name}</strong><span>{SPECTATOR_KIND_LABELS[actor.kind]} · {formatActorLocation(actor)} · {actor.status}</span></div>
                      </li>
                    ))}
                  </ul>
                  {!snapshot.nearby.residents.length && !snapshot.nearby.players.length && (
                    <p className="spectator-empty">附近暂时没有其他角色。</p>
                  )}
                </section>

                <section>
                  <div className="spectator-panel-heading">
                    <div><p>OBSERVED EVENTS</p><h2>最近观察</h2></div>
                  </div>
                  {snapshot.recent_events?.length ? (
                    <ol className="spectator-timeline">
                      {snapshot.recent_events.slice(0, 8).map((entry, index) => (
                        <li key={`${entry.at}:${index}`}><time>{readableTime(entry.at)}</time><span>{entry.summary}</span></li>
                      ))}
                    </ol>
                  ) : <p className="spectator-empty">尚未观察到新的公开事件。</p>}
                </section>
              </aside>
            </div>

            <footer className="spectator-footnote">
              视角刷新于 {readableTime(snapshot.generated_at)}。系统不会展示模型隐藏推理、系统提示、私聊内容或控制 token。
            </footer>
          </>
        )}
      </div>
    </main>
  )
}
