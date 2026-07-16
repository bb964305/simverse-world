import { useState, useEffect, useCallback } from 'react'
import {
  getAdminProposals, approveAdminProposal, rejectAdminProposal, revertAdminProposal,
  type WorldProposal,
} from '../../services/api'

// Admin world-change proposal review (P3, spec §7). References EventsPanel's
// list layout; the approve/reject/revert interaction is bespoke (EventsPanel has
// no review actions).

const RISK_COLOR: Record<string, string> = { low: '#10b981', medium: '#f59e0b', high: '#ef4444' }

export function ProposalsPanel({ token }: { token: string }) {
  const [proposals, setProposals] = useState<WorldProposal[]>([])
  const [filter, setFilter] = useState('pending')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    getAdminProposals(token, filter || undefined)
      .then((r) => setProposals(r.proposals))
      .catch(() => setNotice({ ok: false, text: '加载失败' }))
      .finally(() => setLoading(false))
  }, [token, filter])

  useEffect(() => { load() }, [load])

  const act = async (id: string, action: 'approve' | 'reject' | 'revert') => {
    setBusy(id); setNotice(null)
    try {
      if (action === 'approve') await approveAdminProposal(token, id)
      else if (action === 'reject') await rejectAdminProposal(token, id)
      else await revertAdminProposal(token, id)
      setNotice({ ok: true, text: action === 'approve' ? '已批准并应用' : action === 'reject' ? '已驳回（燃料退回金库）' : '已回滚' })
      load()
    } catch (e) {
      setNotice({ ok: false, text: e instanceof Error ? e.message : '操作失败' })
    } finally { setBusy(null) }
  }

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>🌍 世界变更提案审核</h1>

      <div style={{ display: 'flex', gap: 8, marginBottom: 16, alignItems: 'center' }}>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{
          background: 'var(--bg-input)', color: 'var(--text)', border: '1px solid var(--border)',
          borderRadius: 8, padding: '6px 10px', fontSize: 13,
        }}>
          {['pending', 'applied', 'rejected', 'reverted', 'failed', ''].map((s) => (
            <option key={s} value={s}>{s || '全部'}</option>
          ))}
        </select>
        <button onClick={load} style={{
          background: 'none', color: 'var(--text)', border: '1px solid var(--border)',
          borderRadius: 8, padding: '6px 12px', fontSize: 13, cursor: 'pointer',
        }}>刷新</button>
        {notice && <span style={{ fontSize: 12, color: notice.ok ? '#10b981' : '#ef4444' }}>{notice.text}</span>}
      </div>

      {loading ? <div style={{ color: 'var(--text-muted)' }}>加载中…</div> : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {proposals.length === 0 && <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无提案</div>}
          {proposals.map((p) => (
            <div key={p.id} style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 14 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                <span style={{ fontWeight: 700 }}>{p.title}</span>
                <span style={{
                  fontSize: 11, padding: '2px 8px', borderRadius: 999,
                  border: `1px solid ${RISK_COLOR[p.risk_level] || 'var(--border)'}`,
                  color: RISK_COLOR[p.risk_level] || 'var(--text-muted)',
                }}>{p.risk_level} 风险</span>
                <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{p.kind} · {p.status}</span>
                {p.author_slug && <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>by {p.author_slug}</span>}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 8, whiteSpace: 'pre-wrap' }}>{p.rationale_md}</div>
              <pre style={{
                fontSize: 11, color: 'var(--text-muted)', background: 'var(--bg-input)',
                borderRadius: 6, padding: 8, overflowX: 'auto', margin: '0 0 8px',
              }}>{JSON.stringify(p.patch, null, 2)}</pre>
              {p.cost_sc > 0 && <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>燃料 {p.cost_sc} 🪙（金库冻结）</div>}
              <div style={{ display: 'flex', gap: 8 }}>
                {p.status === 'pending' && (
                  <>
                    <button disabled={busy === p.id} onClick={() => void act(p.id, 'approve')} style={btn('#10b981')}>批准并应用</button>
                    <button disabled={busy === p.id} onClick={() => void act(p.id, 'reject')} style={btn('#ef4444')}>驳回</button>
                  </>
                )}
                {p.status === 'applied' && (
                  <button disabled={busy === p.id} onClick={() => void act(p.id, 'revert')} style={btn('#f59e0b')}>回滚</button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function btn(color: string) {
  return {
    background: 'none', color, border: `1px solid ${color}66`, borderRadius: 6,
    padding: '6px 12px', fontSize: 12, fontWeight: 600, cursor: 'pointer',
  } as const
}
