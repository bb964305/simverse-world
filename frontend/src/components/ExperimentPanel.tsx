import { useState, useEffect, useCallback, useRef, type CSSProperties } from 'react'
import ReactMarkdown from 'react-markdown'
import { bridge } from '../game/phaserBridge'
import { onWSMessage } from '../services/ws'
import { useGameStore } from '../stores/gameStore'
import {
  listLabResearchers, createLabTask, getLabTasks, getLabTask,
  cancelLabTask, acceptLabResult, rejectLabResult, getLabRunSteps, respondLabApproval,
  getWorldLocations, getMe,
  type LabResearcher, type LabTask, type LabRun, type LabRunStep, type LabArtifact, type WorldLocation,
} from '../services/api'
import { resolveLabDisplay } from '../services/labState'
import { artifactKindBadge, artifactStatusBadges } from '../services/labArtifactBadges'

interface LabApproval { id: string; tool?: string | null; summary?: string; status?: string }

// ExperimentPanel — Lab / 实验楼 entry panel (spec §9). Self-mounted in TopNav.
// Panel-local state (spec sanctions this over a store slice); live run steps come
// via the ws.ts onWSMessage fan-out with a REST poll fallback.

type LabTab = 'publish' | 'live' | 'artifacts'
const ACCENT = '#14b8a6'
const SCOPES = ['web_search', 'browse', 'code', 'http']

const TABS: { key: LabTab; label: string }[] = [
  { key: 'publish', label: '发布委托' },
  { key: 'live', label: '运行直播' },
  { key: 'artifacts', label: '产物 & 提案墙' },
]

const inputStyle: CSSProperties = {
  width: '100%', background: '#0b0b0d', color: 'var(--text)',
  border: '1px solid var(--border)', borderRadius: 8, padding: '8px 10px',
  fontSize: 13, boxSizing: 'border-box',
}

function errText(e: unknown): string {
  const m = e instanceof Error ? e.message : String(e)
  if (m.includes('402') || m.includes('balance')) return '余额不足'
  if (m.includes('503')) return '实验楼暂时关闭'
  return '操作失败，请稍后重试'
}

export function ExperimentPanel() {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<LabTab>('publish')
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
    closeButtonRef.current?.focus()
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  if (!open) return null

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
        <button ref={closeButtonRef} onClick={() => setOpen(false)} className="game-dialog-close" aria-label="关闭实验楼">✕</button>
      </div>

      <div style={{ display: 'flex', gap: 4, padding: '8px 20px 0', borderBottom: '1px solid var(--border)' }}>
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)} style={{
            background: 'none', border: 'none', cursor: 'pointer', padding: '8px 10px',
            fontSize: 13, fontWeight: 600, color: tab === t.key ? ACCENT : 'var(--text-muted)',
            borderBottom: `2px solid ${tab === t.key ? ACCENT : 'transparent'}`,
          }}>{t.label}</button>
        ))}
      </div>

      <div style={{ padding: 20 }}>
        {tab === 'publish' && <PublishTab onPublished={() => { getMe().then((m) => updateBalance(m.soul_coin_balance)).catch(() => {}) }} />}
        {tab === 'live' && <LiveTab onBalanceChange={() => { getMe().then((m) => updateBalance(m.soul_coin_balance)).catch(() => {}) }} />}
        {tab === 'artifacts' && <ArtifactsTab />}
      </div>
      </section>
    </div>
  )
}

// ── Publish ───────────────────────────────────────────────────────────

function PublishTab({ onPublished }: { onPublished: () => void }) {
  const [researchers, setResearchers] = useState<LabResearcher[]>([])
  const [title, setTitle] = useState('')
  const [brief, setBrief] = useState('')
  const [scopes, setScopes] = useState<string[]>(['web_search'])
  const [reward, setReward] = useState(50)
  const [researcher, setResearcher] = useState<string>('')  // '' = open recruitment
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => { listLabResearchers().then((r) => setResearchers(r.researchers)).catch(() => {}) }, [])

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
      <input style={inputStyle} placeholder="任务标题" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
      <textarea style={{ ...inputStyle, minHeight: 90, resize: 'vertical' }} placeholder="任务说明（自然语言描述你想让研究员做什么）"
        value={brief} onChange={(e) => setBrief(e.target.value)} />
      <div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>能力域（scope，越多费用越高）</div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {SCOPES.map((s) => (
            <label key={s} style={{
              fontSize: 12, padding: '4px 10px', borderRadius: 999, cursor: 'pointer',
              border: `1px solid ${scopes.includes(s) ? ACCENT : 'var(--border)'}`,
              color: scopes.includes(s) ? ACCENT : 'var(--text-muted)',
            }}>
              <input type="checkbox" checked={scopes.includes(s)} onChange={() => toggleScope(s)} style={{ display: 'none' }} />
              {s}
            </label>
          ))}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>研究员</div>
          <select style={inputStyle} value={researcher} onChange={(e) => setResearcher(e.target.value)}>
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
          <input style={inputStyle} type="number" min={1} value={reward}
            onChange={(e) => setReward(Math.max(1, parseInt(e.target.value || '0', 10)))} />
        </div>
      </div>
      {notice && <div style={{ fontSize: 12, color: notice.ok ? ACCENT : '#ef4444' }}>{notice.text}</div>}
      <button onClick={() => void submit()} disabled={busy} style={{
        background: ACCENT, color: '#04110f', border: 'none', borderRadius: 8,
        padding: '10px', fontWeight: 700, cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.6 : 1,
      }}>{busy ? '发布中…' : '发布委托'}</button>
    </div>
  )
}

// ── Live run ──────────────────────────────────────────────────────────

function LiveTab({ onBalanceChange }: { onBalanceChange: () => void }) {
  const [tasks, setTasks] = useState<LabTask[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [run, setRun] = useState<LabRun | null>(null)
  const [steps, setSteps] = useState<LabRunStep[]>([])
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
        const { run: r } = await getLabTask(selected)
        if (cancelled) return
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

  return (
    <div className="game-lab-split" style={{ display: 'flex', gap: 12 }}>
      <div style={{ width: 200, borderRight: '1px solid var(--border)', paddingRight: 12 }}>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8 }}>我的委托</div>
        {tasks.length === 0 && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>还没有委托</div>}
        {tasks.map((t) => (
          <button key={t.id} onClick={() => selectTask(t.id)} style={{
            display: 'block', width: '100%', textAlign: 'left', background: selected === t.id ? '#14b8a614' : 'none',
            border: 'none', borderRadius: 6, padding: '6px 8px', cursor: 'pointer', marginBottom: 4,
            color: 'var(--text)', fontSize: 12,
          }}>
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
              // Dual state: Task and Run badges render separately (never merged);
              // a terminal run clears transient phase/approval (art-spec 6 rules).
              const d = resolveLabDisplay({ taskStatus: task?.status, runStatus: run.status })
              return <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <span>任务 <b style={{ color: d.task.known ? ACCENT : '#a1a1aa' }}>{d.task.label}</b></span>
                {d.run && <span>运行 <b style={{ color: d.run.known ? ACCENT : '#a1a1aa' }}>{d.run.label}</b></span>}
                {d.phase === 'verifying' && <span style={{ color: '#3b82f6' }}>验证中</span>}
                <span>适配器 {run.adapter}</span>
              </div>
            })()}
            {run && (run.approvals as LabApproval[] | undefined || [])
              .filter((a) => a.status === 'pending').map((a) => (
              <div key={a.id} style={{
                border: '1px solid #f59e0b66', background: '#f59e0b12', borderRadius: 8,
                padding: 10, marginBottom: 8,
              }}>
                <div style={{ fontSize: 12, marginBottom: 6 }}>⚠️ 敏感动作待批准：{a.summary || a.tool}</div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button onClick={async () => { await respondLabApproval(run.id, a.id, true); }} style={btn(ACCENT)}>批准</button>
                  <button onClick={async () => { await respondLabApproval(run.id, a.id, false); }} style={btn('#ef4444')}>拒绝</button>
                </div>
              </div>
            ))}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 300, overflowY: 'auto' }}>
              {steps.map((s) => (
                <div key={`${s.run_id}-${s.seq}`} style={{ fontSize: 12, lineHeight: 1.5 }}>
                  <span style={{ color: ACCENT }}>[{s.phase}]</span>{s.tool ? ` ${s.tool}` : ''} {s.summary}
                </div>
              ))}
              {steps.length === 0 && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>暂无步骤</div>}
            </div>
            <TaskActions task={tasks.find((t) => t.id === selected)} onSettle={settle} onCancel={cancel} />
          </div>
        )}
      </div>
    </div>
  )
}

function TaskActions({ task, onSettle, onCancel }: {
  task: LabTask | undefined
  onSettle: (id: string, accept: boolean) => void
  onCancel: (id: string) => void
}) {
  if (!task) return null
  return (
    <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
      {task.status === 'review' && (
        <>
          <button onClick={() => onSettle(task.id, true)} style={btn(ACCENT)}>满意，放款</button>
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
  return {
    background: 'none', color, border: `1px solid ${color}66`, borderRadius: 6,
    padding: '6px 12px', fontSize: 12, fontWeight: 600, cursor: 'pointer',
  }
}

// ── Artifacts & proposal wall ─────────────────────────────────────────

function ArtifactsTab() {
  const [tasks, setTasks] = useState<LabTask[]>([])
  const [artifacts, setArtifacts] = useState<Record<string, LabArtifact[]>>({})
  const [worldChanges, setWorldChanges] = useState<WorldLocation[]>([])

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
  }, [])

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
              {a.unlocked && a.text_md && (
                <div style={{ fontSize: 12, lineHeight: 1.6 }}><ReactMarkdown>{a.text_md}</ReactMarkdown></div>
              )}
              {a.unlocked && a.uri && (
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                  外链（请谨慎，勿直接点击不明来源）：<span style={{ wordBreak: 'break-all' }}>{a.uri}</span>
                </div>
              )}
              {!a.unlocked && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>放款后解锁</div>}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
