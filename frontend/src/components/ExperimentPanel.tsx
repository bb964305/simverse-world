import { useState, useEffect, useCallback, useRef, type CSSProperties } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import { bridge } from '../game/phaserBridge'
import { onWSMessage } from '../services/ws'
import { useGameStore } from '../stores/gameStore'
import {
  listLabResearchers, createLabTask, quoteLabTask, getLabTasks, getLabTask, getLabRun,
  cancelLabTask, acceptLabResult, rejectLabResult, getLabRunSteps, respondLabApproval,
  getWorldLocations, getMe, downloadLabArtifact,
  getLabStatus, getLabMarketCandidates, nominateLabMarketCandidate,
  type LabResearcher, type LabTask, type LabRun, type LabRunStep, type LabArtifact,
  type LabApproval, type LabTaskQuote, type WorldLocation, type LabStatus,
  type LabMarketCandidate,
} from '../services/api'
import { formatLabMemory, formatLabRunProfile } from '../services/labModel'
import { resolveLabDisplay, selectLabTask, canDecideApproval, approvalId } from '../services/labState'
import { artifactKindBadge, artifactStatusBadges } from '../services/labArtifactBadges'
import { LabTimelineLive } from './LabTimelineLive'
import { labInput, labTab, labChip, labPublishBtn, labTaskRow, labBtn, labClose } from './labControls'

// ExperimentPanel — Lab / 实验楼 entry panel (spec §9). Self-mounted in TopNav.
// Panel-local state (spec sanctions this over a store slice); live run steps come
// via the ws.ts onWSMessage fan-out with a REST poll fallback.

type LabTab = 'visit' | 'publish' | 'live' | 'artifacts'
const ACCENT = '#14b8a6'

// Defense-in-depth for artifact bodies (gap #10): even released (clean+verified)
// Markdown must not create a directly clickable untrusted link or issue a remote
// image request. Links render as inert text; images render as an inert
// placeholder that never fetches the remote resource.
const INERT_MD_COMPONENTS: Components = {
  a: ({ children }) => <span style={{ textDecoration: 'underline', textUnderlineOffset: 2 }}>{children}</span>,
  img: ({ alt }) => <span style={{ color: 'var(--text-muted)' }}>🖼️ {alt || '图片（远程加载已屏蔽）'}</span>,
}
const TABS: { key: LabTab; label: string }[] = [
  { key: 'visit', label: '参观 & 状态' },
  { key: 'publish', label: '发布委托' },
  { key: 'live', label: '运行直播' },
  { key: 'artifacts', label: '产物 & 提案墙' },
]

function errText(e: unknown): string {
  const m = e instanceof Error ? e.message : String(e)
  if (m.includes('402') || m.includes('balance')) return '余额不足'
  if (m.includes('403') || m.includes('closed beta')) return '当前账号尚未获得封闭测试资格'
  if (m.includes('503')) return '实验楼暂时关闭'
  return '操作失败，请稍后重试'
}

export function ExperimentPanel() {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<LabTab>('visit')
  const [status, setStatus] = useState<LabStatus | null>(null)
  const updateBalance = useGameStore((s) => s.updateBalance)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const unsubOpen = bridge.on('experiment:open', () => {
      bridge.emit('bulletin:close')
      setOpen(true)
    })
    const unsubClose = bridge.on('experiment:close', () => setOpen(false))
    return () => { unsubOpen(); unsubClose() }
  }, [])

  useEffect(() => {
    if (!open) return
    getLabStatus().then(setStatus).catch(() => setStatus(null))
    closeButtonRef.current?.focus()
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  if (!open) return null
  const visibleTab: LabTab = status && !status.publish_allowed && tab === 'publish'
    ? 'visit'
    : tab

  return (
    <div
      className="game-modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) setOpen(false) }}
    >
      <section
        className="game-modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="experiment-dialog-title"
        style={{ borderColor: `${ACCENT}55` }}
      >
      <div className="game-dialog-header" style={{
        padding: '16px 20px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        background: '#14b8a608',
      }}>
        <div>
          <div id="experiment-dialog-title" style={{ fontWeight: 800, fontSize: 15, color: ACCENT }}>🧪 实验楼</div>
          <div style={{ color: 'var(--text-muted)', fontSize: 11, marginTop: 2 }}>
            发布真实委托 · 观看研究员运行 · 领取产物与世界提案
          </div>
        </div>
        <span style={{
          marginLeft: 'auto', marginRight: 8, borderRadius: 999, padding: '3px 8px',
          fontSize: 10, border: '1px solid var(--border)',
          color: status?.publish_allowed ? ACCENT : 'var(--text-muted)',
        }}>
          {status?.publish_allowed ? '封闭测试可用' : status?.deploy_enabled ? '参观开放 · 发布受限' : '参观开放 · 筹备中'}
        </span>
        <button ref={closeButtonRef} onClick={() => setOpen(false)} className="game-dialog-close" style={labClose()} aria-label="关闭实验楼">✕</button>
      </div>

      <div style={{ display: 'flex', gap: 4, padding: '8px 20px 0', borderBottom: '1px solid var(--border)' }}>
        {TABS.filter((t) => t.key !== 'publish' || status?.publish_allowed).map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)} style={labTab(visibleTab === t.key)}>{t.label}</button>
        ))}
      </div>

      <div style={{ padding: 20 }}>
        {visibleTab === 'visit' && <VisitTab status={status} />}
        {visibleTab === 'publish' && <PublishTab onPublished={() => { getMe().then((m) => updateBalance(m.soul_coin_balance)).catch(() => {}) }} />}
        {visibleTab === 'live' && <LiveTab onBalanceChange={() => { getMe().then((m) => updateBalance(m.soul_coin_balance)).catch(() => {}) }} />}
        {visibleTab === 'artifacts' && <ArtifactsTab />}
      </div>
      </section>
    </div>
  )
}

function VisitTab({ status }: { status: LabStatus | null }) {
  if (!status) {
    return <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>正在读取实验楼运行状态…</div>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{
        border: `1px solid ${ACCENT}44`, borderRadius: 10, padding: 14,
        background: '#14b8a608', fontSize: 12, lineHeight: 1.7,
      }}>
        <div style={{ fontWeight: 800, color: ACCENT, marginBottom: 4 }}>
          {status.publish_allowed ? '实验委托通道已为你开放' : '实验楼参观区已开放'}
        </div>
        {status.publish_allowed
          ? '你可以发布 code 范围的真实研究委托。资金先进入托管，产物经过审核后再放款。'
          : status.beta_mode && !status.beta_admitted
            ? '当前处于封闭测试阶段。你仍可查看运行方式和已完成成果，发布权限由管理员准入。'
            : '真实执行通道尚在筹备或暂停中。关闭状态不会影响已完成任务与产物读取。'}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8 }}>
        {status.checks.map((check) => (
          <div key={check.key} style={{
            border: '1px solid var(--border)', borderRadius: 8, padding: '9px 11px',
            display: 'flex', alignItems: 'center', gap: 8, fontSize: 11,
          }}>
            <span>{check.ok ? '✅' : check.optional ? '◌' : '⏳'}</span>
            <span style={{ color: check.ok ? 'var(--text-secondary)' : 'var(--text-muted)' }}>{check.label}</span>
          </div>
        ))}
      </div>
      <div style={{ color: 'var(--text-muted)', fontSize: 11, lineHeight: 1.7 }}>
        当前适配器：{status.adapter} · 能力域：{status.available_scopes.join('、') || '暂无'}。研究成果只能进入审核队列，不会直接修改小镇。
      </div>
    </div>
  )
}

// ── Publish ───────────────────────────────────────────────────────────

function PublishTab({ onPublished }: { onPublished: () => void }) {
  const [researchers, setResearchers] = useState<LabResearcher[]>([])
  const [title, setTitle] = useState('')
  const [brief, setBrief] = useState('')
  const [scopes, setScopes] = useState<string[]>(['code'])
  const [availableScopes, setAvailableScopes] = useState<string[]>(['code'])
  const [reward, setReward] = useState(50)
  const [researcher, setResearcher] = useState<string>('')  // '' = open recruitment
  const [busy, setBusy] = useState(false)
  const [quote, setQuote] = useState<LabTaskQuote | null>(null)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => { listLabResearchers().then((r) => setResearchers(r.researchers)).catch(() => {}) }, [])
  useEffect(() => {
    let cancelled = false
    setQuote(null)
    const timer = window.setTimeout(() => {
      quoteLabTask({ reward_sc: reward, scopes })
        .then((value) => {
          if (cancelled) return
          setQuote(value)
          setAvailableScopes(value.available_scopes)
        })
        .catch(() => { if (!cancelled) setQuote(null) })
    }, 150)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [reward, scopes])

  const toggleScope = (s: string) =>
    setScopes((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]))

  const submit = async () => {
    if (!title.trim()) { setNotice({ ok: false, text: '请填写标题' }); return }
    if (scopes.length === 0) { setNotice({ ok: false, text: '至少选择一个能力域' }); return }
    if (reward <= 0) { setNotice({ ok: false, text: '悬赏需大于 0' }); return }
    setBusy(true); setNotice(null)
    try {
      await createLabTask({
        title: title.trim(), brief_md: brief, scopes, reward_sc: reward,
        researcher_slug: researcher || null,
      })
      setNotice({ ok: true, text: `委托已发布（冻结 ${reward} + 平台费）` })
      setTitle(''); setBrief('')
      onPublished()
    } catch (e) {
      setNotice({ ok: false, text: errText(e) })
    } finally { setBusy(false) }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <input style={labInput} placeholder="任务标题" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
      <textarea style={{ ...labInput, minHeight: 90, resize: 'vertical' }} placeholder="任务说明（自然语言描述你想让研究员做什么）"
        value={brief} onChange={(e) => setBrief(e.target.value)} />
      <div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>当前执行环境允许的能力域</div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {availableScopes.map((s) => (
            <label key={s} style={labChip(scopes.includes(s))}>
              <input type="checkbox" checked={scopes.includes(s)} onChange={() => toggleScope(s)} style={{ display: 'none' }} />
              {s}
            </label>
          ))}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>研究员</div>
          <select style={labInput} value={researcher} onChange={(e) => setResearcher(e.target.value)}>
            <option value="">公开招募（自动分派）</option>
            {researchers.map((r) => (
              <option key={r.slug} value={r.slug} disabled={r.busy}>
                {r.name}{r.tier ? `（${r.tier}）` : ''}{r.busy ? ' · 忙' : ''}
              </option>
            ))}
          </select>
        </div>
        <div style={{ width: 140 }}>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>悬赏 🪙</div>
          <input style={labInput} type="number" min={1} value={reward}
            onChange={(e) => setReward(Math.max(1, parseInt(e.target.value || '0', 10)))} />
        </div>
      </div>
      {quote && (
        <div style={{
          borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)',
          padding: '10px 0', display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 220px), 1fr))',
          gap: '6px 16px', fontSize: 12, overflowWrap: 'anywhere',
        }}>
          <div style={{ color: 'var(--text-secondary)' }}>
            {quote.adapter} · {quote.model_tier === 'high' ? '高报酬档' : '低报酬档'} · <b>{quote.model_name}</b>
          </div>
          <div style={{ color: 'var(--text-secondary)' }}>
            {quote.resource_cpu_cores} 核 · {formatLabMemory(quote.resource_memory_mb)}
          </div>
          <div style={{ color: 'var(--text-muted)' }}>
            平台费 {quote.platform_fee_sc} SC · 共冻结 {quote.total_hold_sc} SC
          </div>
          <div style={{ color: quote.eligible ? 'var(--text-muted)' : '#ef4444' }}>
            {quote.unsupported_scopes.length > 0
              ? `${quote.adapter} 暂不支持 ${quote.unsupported_scopes.join('、')}`
              : quote.eligible
              ? quote.model_tier === 'high'
                ? '已启用高报酬模型与资源'
                : `达到 ${quote.pro_min_reward_sc} SC 自动升级高报酬档`
              : `当前能力域最低悬赏 ${quote.minimum_reward_sc} SC`}
          </div>
        </div>
      )}
      {notice && <div style={{ fontSize: 12, color: notice.ok ? ACCENT : '#ef4444' }}>{notice.text}</div>}
      <button onClick={() => void submit()} disabled={busy || !quote?.eligible} style={labPublishBtn(busy || !quote?.eligible)}>{busy ? '发布中…' : '发布委托'}</button>
    </div>
  )
}

// ── Live run ──────────────────────────────────────────────────────────

function LiveTab({ onBalanceChange }: { onBalanceChange: () => void }) {
  const [tasks, setTasks] = useState<LabTask[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [run, setRun] = useState<LabRun | null>(null)
  const [steps, setSteps] = useState<LabRunStep[]>([])
  const [runArtifacts, setRunArtifacts] = useState<LabArtifact[]>([])
  const lastSeq = useRef(0)
  const runRef = useRef<LabRun | null>(null)

  const loadTasks = useCallback(() => {
    getLabTasks('mine').then((r) => setTasks(r.tasks)).catch(() => {})
  }, [])
  useEffect(() => { loadTasks() }, [loadTasks])

  // Load selected task detail + poll steps; also append live via WS fan-out.
  useEffect(() => {
    if (!selected) return
    let cancelled = false
    const pull = async () => {
      try {
        const detail = await getLabTask(selected)
        let r = detail.run
        if (cancelled) return
        setRunArtifacts(detail.artifacts)
        if (r) {
          // Load the server-authoritative run projection so approval controls
          // reflect allowed_actions/can_decide, not the legacy approvals blob.
          try { r = await getLabRun(r.id) } catch { /* keep the getLabTask run */ }
          if (cancelled) return
        }
        runRef.current = r
        setRun(r)
        if (r) {
          const { steps: s } = await getLabRunSteps(r.id, lastSeq.current)
          if (cancelled) return
          if (s.length) {
            // WS may advance lastSeq while this request is in flight. Only
            // merge genuinely newer steps and never move the cursor backward.
            const fresh = s.filter((step) => step.seq > lastSeq.current)
            lastSeq.current = Math.max(lastSeq.current, ...s.map((step) => step.seq))
            if (fresh.length) {
              setSteps((prev) => {
                const seen = new Set(prev.map((step) => `${step.run_id}:${step.seq}`))
                return [...prev, ...fresh.filter((step) => !seen.has(`${step.run_id}:${step.seq}`))]
              })
            }
          }
        }
      } catch { /* ignore */ }
    }
    void pull()
    const timer = setInterval(pull, 2000)
    const unsub = onWSMessage((data) => {
      const activeRun = runRef.current
      if (data.type === 'lab_run_step' && activeRun && data.run_id === activeRun.id) {
        const seq = Number(data.seq)
        if (seq > lastSeq.current) {
          lastSeq.current = seq
          setSteps((prev) => [...prev, {
            id: String(data.seq), run_id: String(data.run_id), seq,
            phase: String(data.phase), tool: (data.tool as string) ?? null,
            summary: String(data.summary ?? ''), payload: {}, created_at: null,
          }])
        }
      }
      if (data.type === 'lab_task_update' && data.task_id === selected) loadTasks()
    })
    return () => { cancelled = true; clearInterval(timer); unsub() }
  }, [selected, loadTasks])

  const selectTask = (id: string) => {
    lastSeq.current = 0
    runRef.current = null
    setRun(null)
    setSteps([])
    setRunArtifacts([])
    setSelected(id)
  }

  const settle = async (id: string, accept: boolean) => {
    try {
      if (accept) await acceptLabResult(id); else await rejectLabResult(id)
      onBalanceChange(); loadTasks()
    } catch { /* ignore */ }
  }
  const cancel = async (id: string) => {
    try { await cancelLabTask(id); onBalanceChange(); loadTasks() } catch { /* ignore */ }
  }

  // Derive the selected task once; status display and TaskActions share it.
  const selectedTask = selectLabTask(tasks, selected)

  return (
    <div className="game-lab-split" style={{ display: 'flex', gap: 12 }}>
      <div style={{ width: 200, borderRight: '1px solid var(--border)', paddingRight: 12 }}>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8 }}>我的委托</div>
        {tasks.length === 0 && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>还没有委托</div>}
        {tasks.map((t) => (
          <button key={t.id} onClick={() => selectTask(t.id)} style={labTaskRow(selected === t.id)}>
            <div style={{ fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{t.title}</div>
            {(() => { const b = resolveLabDisplay({ taskStatus: t.status }).task
              return <div style={{ color: b.known ? ACCENT : 'var(--text-muted)', fontSize: 11 }}>{b.label}</div> })()}
          </button>
        ))}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {!selected && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>选择一个委托查看运行直播</div>}
        {selected && (
          <div>
            {run && (() => {
              // Four-track timeline: Task, Run, event-phase, and connection stay
              // SEPARATE tracks (never merged); a terminal run clears transient
              // phase/approval (art-spec 6 rules). `verifying` overlays only a
              // running run; the connection track freezes animations truthfully.
              const latest = steps.length ? steps[steps.length - 1] : null
              const d = resolveLabDisplay({
                taskStatus: selectedTask?.status, runStatus: run.status,
                eventPhase: latest?.phase === 'verifying' ? 'verifying' : null,
              })
              return <div style={{ marginBottom: 8 }}>
                <LabTimelineLive display={d} />
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  适配器 {run.adapter}{formatLabRunProfile(run) ? ` · ${formatLabRunProfile(run)}` : ''}
                </div>
              </div>
            })()}
            {run && ((run.approvals as LabApproval[] | undefined) || [])
              // Render approve/deny ONLY when the SERVER says this viewer may
              // decide (allowed_actions/can_decide); observers and non-owners get
              // no controls. Legacy flag-off falls back to pending status.
              .filter((a) => canDecideApproval(a)).map((a) => {
              const aid = approvalId(a)
              return (
              <div key={aid} style={{
                border: '1px solid #f59e0b66', background: '#f59e0b12', borderRadius: 8,
                padding: 10, marginBottom: 8,
              }}>
                <div style={{ fontSize: 12, marginBottom: 6 }}>⚠️ 敏感动作待批准：{a.summary || a.tool}</div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button onClick={async () => { if (aid) await respondLabApproval(run.id, aid, true); }} style={btn(ACCENT)}>批准</button>
                  <button onClick={async () => { if (aid) await respondLabApproval(run.id, aid, false); }} style={btn('#ef4444')}>拒绝</button>
                </div>
              </div>
              )
            })}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 300, overflowY: 'auto' }}>
              {steps.map((s) => (
                <div key={`${s.run_id}-${s.seq}`} style={{ fontSize: 12, lineHeight: 1.5 }}>
                  <span style={{ color: ACCENT }}>[{s.phase}]</span>{s.tool ? ` ${s.tool}` : ''} {s.summary}
                </div>
              ))}
              {steps.length === 0 && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>暂无步骤</div>}
            </div>
            {runArtifacts.length > 0 && (
              <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {runArtifacts.map((artifact) => (
                  <div key={artifact.id} style={{ fontSize: 11, color: 'var(--text-muted)', display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    <span>{artifactKindBadge(artifact.kind).icon} {artifact.title}</span>
                    {artifactStatusBadges(artifact).map((badge) => <span key={badge.key}>{badge.label}</span>)}
                  </div>
                ))}
              </div>
            )}
            <TaskActions task={selectedTask} artifacts={runArtifacts} onSettle={settle} onCancel={cancel} />
          </div>
        )}
      </div>
    </div>
  )
}

function TaskActions({ task, artifacts, onSettle, onCancel }: {
  task: LabTask | undefined
  artifacts: LabArtifact[]
  onSettle: (id: string, accept: boolean) => void
  onCancel: (id: string) => void
}) {
  if (!task) return null
  const productionArtifacts = artifacts.filter((artifact) => artifact.storage_status !== 'legacy')
  const artifactsReady = productionArtifacts.length === 0 || productionArtifacts
    .filter((artifact) => artifact.required !== false)
    .every((artifact) => artifact.storage_status === 'released'
      && artifact.scan_status === 'clean'
      && artifact.verification_status === 'verified')
  return (
    <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
      {task.status === 'review' && (
        <>
          <button onClick={() => onSettle(task.id, true)} style={btn(ACCENT)} disabled={!artifactsReady}>
            {artifactsReady ? '满意，放款' : '产物处理中'}
          </button>
          <button onClick={() => onSettle(task.id, false)} style={btn('#ef4444')} disabled={task.reject_count >= 1}>拒收</button>
        </>
      )}
      {['funded', 'assigned', 'running'].includes(task.status) && (
        <button onClick={() => onCancel(task.id)} style={btn('#f59e0b')}>取消委托</button>
      )}
    </div>
  )
}

function btn(color: string): CSSProperties {
  return labBtn(color)
}

// ── Artifacts & proposal wall ─────────────────────────────────────────

function ArtifactsTab() {
  const [tasks, setTasks] = useState<LabTask[]>([])
  const [artifacts, setArtifacts] = useState<Record<string, LabArtifact[]>>({})
  const [worldChanges, setWorldChanges] = useState<WorldLocation[]>([])
  const [candidates, setCandidates] = useState<LabMarketCandidate[]>([])

  useEffect(() => {
    getLabTasks('mine').then((r) => {
      const done = r.tasks.filter((t) => t.status === 'completed')
      setTasks(done)
      done.forEach((t) => {
        getLabTask(t.id).then((d) => setArtifacts((prev) => ({ ...prev, [t.id]: d.artifacts }))).catch(() => {})
      })
    }).catch(() => {})
    // Proposal wall: buildings the researchers actually added to the town.
    getWorldLocations().then((r) => setWorldChanges(r.locations.filter((l) => l.dynamic))).catch(() => {})
    getLabMarketCandidates().then((r) => setCandidates(r.candidates)).catch(() => {})
  }, [])

  const nominate = async (artifact: LabArtifact) => {
    const candidate = await nominateLabMarketCandidate(artifact.id, {
      title: artifact.title || '实验楼成果',
      summary: '由实验楼产物提交，等待市场产品化与安全审核。',
      offer_type: 'service',
      suggested_price_sc: 5,
    })
    setCandidates((current) => current.some((item) => item.id === candidate.id)
      ? current.map((item) => item.id === candidate.id ? candidate : item)
      : [candidate, ...current])
  }

  const wall = worldChanges.length > 0 && (
    <div style={{ border: '1px solid #14b8a644', borderRadius: 10, padding: 12, background: '#14b8a608' }}>
      <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8, color: ACCENT }}>🌍 小镇因研究员而改变</div>
      {worldChanges.map((l) => (
        <div key={l.slug} style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 4 }}>
          · 新增了 <b>{l.name ?? l.slug}</b>{l.description ? ` —— ${l.description}` : ''}
        </div>
      ))}
    </div>
  )

  if (tasks.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {wall}
        <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.7 }}>
          完成并放款的委托产物会陈列在这里；研究员产出并通过审核的世界变更在上方叙事化展示。
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {wall}
      {tasks.map((t) => (
        <div key={t.id}>
          <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 6 }}>{t.title}</div>
          {(artifacts[t.id] || []).map((a) => (
            <div key={a.id} style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 8 }}>
              <div style={{ fontSize: 12, color: ACCENT, marginBottom: 6, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <span>{artifactKindBadge(a.kind).icon} {a.title}</span>
                <span style={{ color: 'var(--text-muted)' }}>{artifactKindBadge(a.kind).label}</span>
                {a.provenance && <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>来源 {a.provenance}</span>}
                {artifactStatusBadges(a).map((b) => (
                  <span key={b.key} style={{ fontSize: 10, border: '1px solid var(--border)', borderRadius: 4, padding: '0 4px' }}>{b.label}</span>
                ))}
              </div>
              <ArtifactContent
                artifact={a}
                candidate={candidates.find((candidate) => candidate.artifact_id === a.id)}
                onNominate={() => nominate(a)}
              />
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

function ArtifactContent({ artifact, candidate, onNominate }: {
  artifact: LabArtifact
  candidate?: LabMarketCandidate
  onNominate: () => Promise<void>
}) {
  const [preview, setPreview] = useState<string | null>(null)
  const [busy, setBusy] = useState<'preview' | 'download' | 'nominate' | null>(null)
  const [error, setError] = useState(false)
  const canPreview = Boolean(
    artifact.content_type?.startsWith('text/')
    || artifact.content_type === 'application/json',
  )

  const loadPreview = async () => {
    setBusy('preview'); setError(false)
    try {
      const { blob } = await downloadLabArtifact(artifact.id, 'inline')
      setPreview(await blob.text())
    } catch {
      setError(true)
    } finally {
      setBusy(null)
    }
  }

  const download = async () => {
    setBusy('download'); setError(false)
    try {
      const { blob, filename } = await downloadLabArtifact(artifact.id)
      const href = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = href
      link.download = filename
      link.click()
      URL.revokeObjectURL(href)
    } catch {
      setError(true)
    } finally {
      setBusy(null)
    }
  }

  const nominate = async () => {
    setBusy('nominate'); setError(false)
    try { await onNominate() } catch { setError(true) } finally { setBusy(null) }
  }

  if (!artifact.unlocked) {
    const pending = ['pending_upload', 'quarantined'].includes(artifact.storage_status || '')
      || ['pending', 'scanning'].includes(artifact.scan_status || '')
    return <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
      {pending ? '产物处理中' : artifact.storage_status === 'deleted' ? '产物已过期' : '放款后解锁'}
    </div>
  }

  return <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
    <div style={{ display: 'flex', gap: 8 }}>
      {canPreview && (
        <button onClick={() => void loadPreview()} disabled={busy !== null} style={btn(ACCENT)}>
          {busy === 'preview' ? '加载中' : '预览'}
        </button>
      )}
      <button onClick={() => void download()} disabled={busy !== null} style={btn(ACCENT)}>
        {busy === 'download' ? '下载中' : '下载'}
      </button>
      {candidate ? (
        <span style={{ alignSelf: 'center', fontSize: 10, color: candidate.status === 'approved' ? ACCENT : 'var(--text-muted)' }}>
          市场候选：{{ pending: '待审核', approved: '已通过', rejected: '未通过', published: '已发布' }[candidate.status]}
        </span>
      ) : artifact.scan_status === 'clean' && artifact.verification_status === 'verified' ? (
        <button onClick={() => void nominate()} disabled={busy !== null} style={btn('#d97706')}>
          {busy === 'nominate' ? '提交中' : '提交为集市候选'}
        </button>
      ) : null}
    </div>
    {error && <div style={{ fontSize: 11, color: '#ef4444' }}>产物读取失败</div>}
    {preview !== null && (
      <div style={{ fontSize: 12, lineHeight: 1.6 }}>
        <ReactMarkdown components={INERT_MD_COMPONENTS}>{preview}</ReactMarkdown>
      </div>
    )}
  </div>
}
