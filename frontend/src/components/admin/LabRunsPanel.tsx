import { useState, useEffect, useCallback } from 'react'
import {
  getAdminLabRuns, cancelAdminLabRun, getAdminLabStatus, setAdminLabKillSwitch,
  type AdminLabRun, type AdminLabStatus,
} from '../../services/api'

// Admin Lab run monitor + runtime kill switch (P2, spec §5.3/§8).

export function LabRunsPanel({ token }: { token: string }) {
  const [runs, setRuns] = useState<AdminLabRun[]>([])
  const [status, setStatus] = useState<AdminLabStatus | null>(null)
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    Promise.all([getAdminLabRuns(token, filter || undefined), getAdminLabStatus(token)])
      .then(([r, s]) => { setRuns(r.runs); setStatus(s) })
      .catch(() => setNotice('加载失败'))
      .finally(() => setLoading(false))
  }, [token, filter])

  useEffect(() => { load() }, [load])

  const toggleKill = async () => {
    if (!status) return
    try {
      const r = await setAdminLabKillSwitch(token, !status.runtime_enabled)
      setStatus({ ...status, runtime_enabled: r.runtime_enabled })
      setNotice(r.runtime_enabled ? '实验楼已恢复运行' : '实验楼已紧急停用')
    } catch { setNotice('切换失败') }
  }

  const cancel = async (id: string) => {
    try { await cancelAdminLabRun(token, id); load() } catch { setNotice('熔断失败') }
  }

  return (
    <div>
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>🧪 实验楼运行监控</h1>

      {status && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 16, padding: 16, marginBottom: 20,
          border: '1px solid var(--border)', borderRadius: 10, background: 'var(--bg-card)',
        }}>
          <div style={{ fontSize: 13 }}>
            部署开关：<b style={{ color: status.deploy_enabled ? '#10b981' : '#ef4444' }}>{status.deploy_enabled ? '开' : '关'}</b>
            <span style={{ margin: '0 10px', color: 'var(--text-muted)' }}>·</span>
            适配器：<b>{status.adapter}</b>
            <span style={{ margin: '0 10px', color: 'var(--text-muted)' }}>·</span>
            运行时：<b style={{ color: status.runtime_enabled ? '#10b981' : '#ef4444' }}>{status.runtime_enabled ? '运行中' : '已停用'}</b>
          </div>
          <button onClick={() => void toggleKill()} style={{
            marginLeft: 'auto', background: status.runtime_enabled ? '#ef4444' : '#10b981', color: 'white',
            border: 'none', borderRadius: 8, padding: '8px 14px', fontWeight: 700, cursor: 'pointer',
          }}>{status.runtime_enabled ? '🛑 紧急停用（Kill Switch）' : '▶ 恢复运行'}</button>
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
        <select value={filter} onChange={(e) => setFilter(e.target.value)} style={{
          background: 'var(--bg-input)', color: 'var(--text)', border: '1px solid var(--border)',
          borderRadius: 8, padding: '6px 10px', fontSize: 13,
        }}>
          <option value="">全部状态</option>
          {['queued', 'running', 'needs_approval', 'succeeded', 'failed', 'cancelled'].map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <button onClick={load} style={{
          background: 'none', color: 'var(--text)', border: '1px solid var(--border)',
          borderRadius: 8, padding: '6px 12px', fontSize: 13, cursor: 'pointer',
        }}>刷新</button>
        {notice && <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{notice}</span>}
      </div>

      {loading ? <div style={{ color: 'var(--text-muted)' }}>加载中…</div> : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {runs.length === 0 && <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无运行</div>}
          {runs.map((r) => (
            <div key={r.id} style={{
              border: '1px solid var(--border)', borderRadius: 8, padding: 12,
              display: 'flex', alignItems: 'center', gap: 12, fontSize: 13,
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600 }}>{r.researcher_slug} · <span style={{ color: '#14b8a6' }}>{r.status}</span></div>
                <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                  {r.adapter} · scope [{r.scopes.join(', ')}] · 花费 {r.cost_usd_cents}¢/{r.budget_usd_cents}¢
                  {r.error ? ` · 错误：${r.error}` : ''}
                </div>
              </div>
              {!['succeeded', 'failed', 'cancelled'].includes(r.status) && (
                <button onClick={() => void cancel(r.id)} style={{
                  background: 'none', color: '#ef4444', border: '1px solid #ef444466',
                  borderRadius: 6, padding: '6px 12px', fontSize: 12, cursor: 'pointer',
                }}>熔断</button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
