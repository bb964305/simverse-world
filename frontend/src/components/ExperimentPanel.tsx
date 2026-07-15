import { useState, useEffect } from 'react'
import { bridge } from '../game/phaserBridge'

// ExperimentPanel — the Lab / 实验楼 entry panel (spec §9).
//
// P0: skeleton only. Self-mounts in TopNav (like BulletinBoard) and opens on
// the `experiment:open` bridge event, which fires from the TopNav button and
// from ws.ts when the player walks into the experiment building
// (experiment_prompt frame). The three tabs are placeholders here; P1 fills in
// 发布委托 (task form) / 运行直播 (run step stream) / 产物 & 提案墙, and P2/P3
// add the sensitive-action approval UI and the narrative proposal wall.

type LabTab = 'publish' | 'live' | 'artifacts'

const ACCENT = '#14b8a6' // teal — matches the minimap experiment_building zone

const TABS: { key: LabTab; label: string }[] = [
  { key: 'publish', label: '发布委托' },
  { key: 'live', label: '运行直播' },
  { key: 'artifacts', label: '产物 & 提案墙' },
]

export function ExperimentPanel() {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<LabTab>('publish')

  useEffect(() => {
    const unsubOpen = bridge.on('experiment:open', () => setOpen(true))
    const unsubClose = bridge.on('experiment:close', () => setOpen(false))
    return () => {
      unsubOpen()
      unsubClose()
    }
  }, [])

  if (!open) return null

  return (
    <div style={{
      position: 'fixed', top: '50%', left: '50%', transform: 'translate(-50%, -50%)',
      width: 560, maxHeight: '80vh', overflowY: 'auto', zIndex: 25,
      background: '#18181bf5', border: `2px solid ${ACCENT}44`, borderRadius: 16,
      backdropFilter: 'blur(12px)', boxShadow: '0 0 60px rgba(20,184,166,0.1)',
    }}>
      {/* Header */}
      <div style={{
        padding: '16px 20px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        background: '#14b8a608', position: 'sticky', top: 0, zIndex: 1,
      }}>
        <div>
          <div style={{ fontWeight: 800, fontSize: 15, color: ACCENT }}>🧪 实验楼</div>
          <div style={{ color: 'var(--text-muted)', fontSize: 11, marginTop: 2 }}>
            发布真实委托 · 观看研究员运行 · 领取产物与世界提案
          </div>
        </div>
        <button onClick={() => setOpen(false)} style={{
          background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: 18, cursor: 'pointer',
        }}>✕</button>
      </div>

      {/* Tabs */}
      <div style={{
        display: 'flex', gap: 4, padding: '8px 20px 0', borderBottom: '1px solid var(--border)',
      }}>
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              padding: '8px 10px', fontSize: 13, fontWeight: 600,
              color: tab === t.key ? ACCENT : 'var(--text-muted)',
              borderBottom: `2px solid ${tab === t.key ? ACCENT : 'transparent'}`,
            }}
          >{t.label}</button>
        ))}
      </div>

      {/* Body — P0 placeholders; wired up in P1+. */}
      <div style={{ padding: '20px', color: 'var(--text-muted)', fontSize: 13, lineHeight: 1.7 }}>
        {tab === 'publish' && (
          <p>在这里向研究员发布真实世界的委托任务（联网调研 / 执行代码 / 浏览等），
            悬赏代币进入托管，任务完成后验收放款。（即将开放）</p>
        )}
        {tab === 'live' && (
          <p>研究员接单后，这里会逐条直播 Ta 的思考、动作与观察；涉及敏感动作时会请你批准或拒绝。（即将开放）</p>
        )}
        {tab === 'artifacts' && (
          <p>任务产物在放款后于此解锁领取；研究员在冒险中产出、并已通过审核的世界变更也会陈列在这里——小镇因你而改变。（即将开放）</p>
        )}
      </div>
    </div>
  )
}
