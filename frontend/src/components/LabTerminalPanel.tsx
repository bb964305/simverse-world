import { useState, useEffect, useCallback, type CSSProperties } from 'react'
import { bridge } from '../game/phaserBridge'
import {
  getLabTasks, getLabTask,
  getLabRunSteps,
  type LabTask, type LabRun, type LabRunStep,
} from '../services/api'
import { resolveLabDisplay } from '../services/labState'
import { formatLabRunProfile } from '../services/labModel'

// LabTerminalPanel — read-only lab run monitor (Society Expansion §10). Unlike
// ExperimentPanel this issues NO writes: no publish, no settle, no approve. It
// reuses services/api/lab.ts (getLabTasks('mine') + getLabTask) and the shared
// resolveLabDisplay badges. Player-only; TopNav gates the entry behind labEnabled.
// Opened via the `labterminal:open` bridge event; self-mounted in TopNav.

const ACCENT = '#38bdf8' // sky — a read-only shade next to the lab's teal

const muted: CSSProperties = { fontSize: 12, color: 'var(--text-muted)' }
const card: CSSProperties = {
  border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 8,
  background: 'var(--bg-input)',
}

export function LabTerminalPanel() {
  const [open, setOpen] = useState(false)
  const [tasks, setTasks] = useState<LabTask[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [run, setRun] = useState<LabRun | null>(null)
  const [steps, setSteps] = useState<LabRunStep[]>([])
  const [loading, setLoading] = useState(false)

  const loadTasks = useCallback(() => {
    setLoading(true)
    getLabTasks('mine')
      .then((r) => setTasks(r.tasks))
      .catch(() => setTasks([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    const unsubOpen = bridge.on('labterminal:open', () => {
      bridge.emit('bulletin:close')
      bridge.emit('experiment:close')
      bridge.emit('townhall:close')
      setOpen(true)
      setSelected(null)
      setRun(null)
      setSteps([])
      loadTasks()
    })
    const unsubClose = bridge.on('labterminal:close', () => setOpen(false))
    return () => { unsubOpen(); unsubClose() }
  }, [loadTasks])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  // Read-only polling keeps the terminal useful without granting write actions.
  useEffect(() => {
    if (!selected) return
    let cancelled = false
    let activeRunId: string | null = null
    let lastSeq = 0
    const pull = async () => {
      try {
        const detail = await getLabTask(selected)
        if (cancelled) return
        const nextRun = detail.run
        setRun(nextRun)
        if (!nextRun) return
        if (activeRunId !== nextRun.id) {
          activeRunId = nextRun.id
          lastSeq = 0
          setSteps([])
        }
        const result = await getLabRunSteps(nextRun.id, lastSeq)
        if (cancelled || result.steps.length === 0) return
        lastSeq = Math.max(lastSeq, ...result.steps.map((step) => step.seq))
        setSteps((current) => {
          const seen = new Set(current.map((step) => `${step.run_id}:${step.seq}`))
          return [...current, ...result.steps.filter((step) => !seen.has(`${step.run_id}:${step.seq}`))]
        })
      } catch { /* keep the last readable snapshot */ }
    }
    void pull()
    const timer = window.setInterval(() => { void pull() }, 2000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [selected])

  const selectTask = (taskId: string) => {
    setRun(null)
    setSteps([])
    setSelected(taskId)
  }

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
        aria-labelledby="labterminal-dialog-title"
        style={{ borderColor: `${ACCENT}55` }}
      >
        <div className="game-dialog-header" style={{
          padding: '16px 20px', borderBottom: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          background: `${ACCENT}0f`,
        }}>
          <div>
            <div id="labterminal-dialog-title" style={{ fontWeight: 800, fontSize: 15, color: ACCENT }}>📊 实验楼终端</div>
            <div style={{ ...muted, marginTop: 2 }}>只读运行状态 · 无发布 / 放款 / 审批操作</div>
          </div>
          <button onClick={() => setOpen(false)} className="game-dialog-close" aria-label="关闭实验楼终端">✕</button>
        </div>

        <div className="game-lab-split" style={{ display: 'flex', gap: 12, padding: 20 }}>
          <div style={{ width: 200, borderRight: '1px solid var(--border)', paddingRight: 12 }}>
            <div style={{ ...muted, marginBottom: 8 }}>我的委托</div>
            {loading && <div style={muted}>加载中…</div>}
            {!loading && tasks.length === 0 && <div style={muted}>还没有委托</div>}
            {tasks.map((t) => {
              const b = resolveLabDisplay({ taskStatus: t.status }).task
              return (
                <button key={t.id} onClick={() => selectTask(t.id)} style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  background: selected === t.id ? `${ACCENT}14` : 'none',
                  border: 'none', borderRadius: 6, padding: '6px 8px', cursor: 'pointer',
                  marginBottom: 4, color: 'var(--text)', fontSize: 12,
                }}>
                  <div style={{ fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{t.title}</div>
                  <div style={{ color: b.known ? ACCENT : 'var(--text-muted)', fontSize: 11 }}>{b.label}</div>
                </button>
              )
            })}
          </div>

          <div style={{ flex: 1, minWidth: 0 }}>
            {!selected && <div style={muted}>选择一个委托查看运行状态</div>}
            {selected && !run && <div style={muted}>该委托尚未开始运行。</div>}
            {selected && run && (() => {
              const d = resolveLabDisplay({ runStatus: run.status })
              return (
                <div>
                  <div style={{ ...card, display: 'flex', gap: 12, alignItems: 'center' }}>
                    <span style={{ fontSize: 13, fontWeight: 700, color: ACCENT }}>
                      运行状态：{d.run ? d.run.label : '—'}
                    </span>
                    <span style={muted}>
                      适配器 {run.adapter}{formatLabRunProfile(run) ? ` · ${formatLabRunProfile(run)}` : ''}
                    </span>
                    {run.error && <span style={{ fontSize: 12, color: '#ef4444' }}>错误：{run.error}</span>}
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 300, overflowY: 'auto' }}>
                    {steps.map((s) => (
                      <div key={`${s.run_id}-${s.seq}`} style={{ fontSize: 12, lineHeight: 1.5 }}>
                        <span style={{ color: ACCENT }}>[{s.phase}]</span>{s.tool ? ` ${s.tool}` : ''} {s.summary}
                      </div>
                    ))}
                    {steps.length === 0 && <div style={muted}>暂无步骤记录</div>}
                  </div>
                </div>
              )
            })()}
          </div>
        </div>
      </section>
    </div>
  )
}
