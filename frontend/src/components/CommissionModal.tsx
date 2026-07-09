import { useEffect, useState, useCallback } from 'react'
import { getCommissions, acceptCommission, abandonCommission, type CommissionData } from '../services/api'

const KIND_LABEL: Record<string, string> = {
  deliver_message: '带个话',
  chat_topic: '聊个天',
  visit_location: '去个地方',
}

interface Props {
  onClose: () => void
}

export function CommissionModal({ onClose }: Props) {
  const [tab, setTab] = useState<'open' | 'mine'>('open')
  const [items, setItems] = useState<CommissionData[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    getCommissions(tab === 'mine' ? 'mine' : 'open')
      .then((r) => setItems(r.commissions))
      .catch(() => setItems([]))
      .finally(() => setLoading(false))
  }, [tab])

  useEffect(() => { load() }, [load])

  const onAccept = async (id: string) => {
    setError(null)
    try {
      await acceptCommission(id)
      load()
    } catch (e) {
      setError(e instanceof Error && e.message.includes('409') ? '手慢了，已被别人接走' : '接取失败')
    }
  }

  const onAbandon = async (id: string) => {
    try { await abandonCommission(id); load() } catch { /* ignore */ }
  }

  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, zIndex: 200, background: 'rgba(0,0,0,0.6)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 12,
        width: 'min(560px, 92vw)', maxHeight: '80vh', display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', gap: 12, alignItems: 'center' }}>
          <span style={{ fontWeight: 700, fontSize: 15 }}>🗒️ 委托板</span>
          <div style={{ display: 'flex', gap: 4, marginLeft: 8 }}>
            {(['open', 'mine'] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)} style={{
                background: tab === t ? 'var(--accent-red)' : 'transparent', border: 'none',
                color: tab === t ? 'white' : 'var(--text-muted)', padding: '4px 12px',
                borderRadius: 6, fontSize: 12, cursor: 'pointer',
              }}>{t === 'open' ? '可接取' : '我接的'}</button>
            ))}
          </div>
          <button onClick={onClose} style={{ marginLeft: 'auto', background: 'none', border: 'none', color: 'var(--text-muted)', fontSize: 18, cursor: 'pointer' }}>✕</button>
        </div>
        {error && <div style={{ padding: '8px 20px', color: 'var(--accent-red)', fontSize: 12 }}>{error}</div>}
        <div style={{ overflowY: 'auto', flex: 1, padding: '8px 0' }}>
          {loading ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)' }}>加载中…</div>
          ) : items.length === 0 ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>暂无委托</div>
          ) : items.map((c) => (
            <div key={c.id} style={{ padding: '12px 20px', borderBottom: '1px solid var(--border)', display: 'flex', gap: 12, alignItems: 'center' }}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{c.title}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                  {KIND_LABEL[c.kind] ?? c.kind} · 奖励 {c.reward_sc} 🪙 · {c.status}
                </div>
              </div>
              {tab === 'open' ? (
                <button onClick={() => void onAccept(c.id)} style={{
                  background: 'var(--accent-green)', color: '#000', border: 'none',
                  padding: '6px 14px', borderRadius: 6, fontSize: 12, fontWeight: 700, cursor: 'pointer',
                }}>接取</button>
              ) : c.status === 'accepted' ? (
                <button onClick={() => void onAbandon(c.id)} style={{
                  background: 'var(--bg-input)', color: 'var(--text-muted)', border: 'none',
                  padding: '6px 14px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
                }}>放弃</button>
              ) : null}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
