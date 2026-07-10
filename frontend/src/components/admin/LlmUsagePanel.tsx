import { useCallback, useEffect, useState } from 'react'
import { getAdminLlmUsageSummary, type LlmUsageSummary } from '../../services/api'

// ─── Shared sub-components ────────────────────────────────────────

function SectionHeader({ icon, title }: { icon: string; title: string }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      marginBottom: 20, paddingBottom: 12,
      borderBottom: '1px solid var(--border)',
    }}>
      <span style={{ fontSize: 18 }}>{icon}</span>
      <h2 style={{ fontSize: 15, fontWeight: 700, margin: 0, color: 'var(--text-primary)' }}>{title}</h2>
    </div>
  )
}

function SectionCard({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 10, padding: '20px 24px', marginBottom: 16,
    }}>
      {children}
    </div>
  )
}

// ─── Stat Cards ──────────────────────────────────────────────────

interface StatCardProps {
  label: string
  value: string | null
  color?: string
}

function StatCard({ label, value, color }: StatCardProps) {
  return (
    <div style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 10, padding: '16px 20px',
      display: 'flex', flexDirection: 'column', gap: 6, flex: '1 1 160px',
    }}>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 500 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: color ?? 'var(--text-primary)' }}>
        {value ?? '—'}
      </div>
    </div>
  )
}

// ─── Time window options ─────────────────────────────────────────

const WINDOW_OPTIONS = [
  { label: '24h', hours: 24 },
  { label: '72h', hours: 72 },
  { label: '7天', hours: 168 },
  { label: '30天', hours: 720 },
]

const formatCost = (v: number) => `$${v.toFixed(4)}`

// ─── Main LlmUsagePanel ──────────────────────────────────────────

export function LlmUsagePanel({ token }: { token: string }) {
  const [hours, setHours] = useState(24)
  const [summary, setSummary] = useState<LlmUsageSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [hoveredRow, setHoveredRow] = useState<string | null>(null)

  const fetchSummary = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await getAdminLlmUsageSummary(token, hours)
      setSummary(data)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [token, hours])

  useEffect(() => {
    void fetchSummary()
  }, [fetchSummary])

  const scenarios = summary
    ? Object.entries(summary.scenarios).sort((a, b) => b[1].est_cost_usd - a[1].est_cost_usd)
    : []
  const totalCost = summary?.total.est_cost_usd ?? 0

  const gridColumns = '1fr 70px 100px 100px 90px 160px'

  return (
    <div style={{ maxWidth: 900 }}>
      {/* Header row: title + window selector + refresh */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 24 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0, color: 'var(--text-primary)' }}>
          💸 LLM 成本
        </h1>
        <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
          {WINDOW_OPTIONS.map((opt) => {
            const isActive = hours === opt.hours
            return (
              <button
                key={opt.hours}
                onClick={() => setHours(opt.hours)}
                style={{
                  padding: '5px 14px', borderRadius: 6, fontSize: 13,
                  background: 'var(--bg-input)',
                  border: isActive ? '1px solid var(--accent-red)' : '1px solid var(--border)',
                  color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
                  fontWeight: isActive ? 600 : 400,
                  cursor: 'pointer',
                  transition: 'border-color 0.15s, color 0.15s',
                }}
              >
                {opt.label}
              </button>
            )
          })}
        </div>
        <button
          onClick={() => void fetchSummary()}
          disabled={loading}
          style={{
            padding: '5px 14px', borderRadius: 6, fontSize: 13,
            background: 'var(--bg-input)', border: '1px solid var(--border)',
            color: 'var(--text-primary)', cursor: loading ? 'default' : 'pointer',
            opacity: loading ? 0.7 : 1,
          }}
        >
          刷新
        </button>
      </div>

      {/* Stat Cards */}
      {loading ? (
        <div style={{ color: 'var(--text-muted)', padding: '12px 0', fontSize: 13 }}>加载用量数据...</div>
      ) : error || !summary ? (
        <div style={{ color: '#ff6b6b', padding: '12px 0', fontSize: 13 }}>{error ?? '加载失败'}</div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
            <StatCard label="调用次数" value={summary.total.calls.toLocaleString()} />
            <StatCard label="输入 tokens" value={summary.total.input_tokens.toLocaleString()} />
            <StatCard label="输出 tokens" value={summary.total.output_tokens.toLocaleString()} />
            <StatCard label="估算成本" value={formatCost(summary.total.est_cost_usd)} color="var(--accent-green)" />
          </div>

          {/* Per-scenario table */}
          <SectionCard>
            <SectionHeader icon="📊" title="按场景" />

            {scenarios.length === 0 ? (
              <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: '24px 0' }}>
                窗口内无 LLM 调用
              </div>
            ) : (
              <>
                {/* Header row */}
                <div style={{
                  display: 'grid',
                  gridTemplateColumns: gridColumns,
                  gap: 8,
                  padding: '6px 12px',
                  fontSize: 11, fontWeight: 600, color: 'var(--text-muted)',
                  textTransform: 'uppercase', letterSpacing: '0.04em',
                }}>
                  <span>场景</span>
                  <span style={{ textAlign: 'right' }}>调用</span>
                  <span style={{ textAlign: 'right' }}>输入 tok</span>
                  <span style={{ textAlign: 'right' }}>输出 tok</span>
                  <span style={{ textAlign: 'right' }}>成本 $</span>
                  <span>占比</span>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {scenarios.map(([name, s]) => {
                    const ratio = totalCost > 0 ? s.est_cost_usd / totalCost : 0
                    return (
                      <div
                        key={name}
                        onMouseEnter={() => setHoveredRow(name)}
                        onMouseLeave={() => setHoveredRow(null)}
                        style={{
                          display: 'grid',
                          gridTemplateColumns: gridColumns,
                          gap: 8,
                          padding: '8px 12px',
                          background: hoveredRow === name ? 'var(--bg-card)' : 'var(--bg-input)',
                          borderRadius: 6,
                          border: hoveredRow === name ? '1px solid var(--accent-blue)' : '1px solid var(--border)',
                          alignItems: 'center',
                          transition: 'background 0.15s, border-color 0.15s',
                        }}
                      >
                        <span style={{ fontSize: 13, color: 'var(--text-secondary)', fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {name}
                        </span>
                        <span style={{ fontSize: 13, color: 'var(--text-primary)', textAlign: 'right' }}>
                          {s.calls.toLocaleString()}
                        </span>
                        <span style={{ fontSize: 12, color: 'var(--text-muted)', textAlign: 'right' }}>
                          {s.input_tokens.toLocaleString()}
                        </span>
                        <span style={{ fontSize: 12, color: 'var(--text-muted)', textAlign: 'right' }}>
                          {s.output_tokens.toLocaleString()}
                        </span>
                        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--accent-green)', textAlign: 'right' }}>
                          {formatCost(s.est_cost_usd)}
                        </span>
                        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span style={{ flex: 1, height: 8, background: 'var(--border)', borderRadius: 4, overflow: 'hidden' }}>
                            <span style={{
                              display: 'block', height: '100%',
                              width: `${(ratio * 100).toFixed(1)}%`,
                              minWidth: 2,
                              background: 'var(--accent-blue)',
                              borderRadius: 4,
                            }} />
                          </span>
                          <span style={{ fontSize: 11, color: 'var(--text-muted)', width: 42, textAlign: 'right' }}>
                            {(ratio * 100).toFixed(1)}%
                          </span>
                        </span>
                      </div>
                    )
                  })}
                </div>
              </>
            )}
          </SectionCard>

          {/* Footnote */}
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            成本为 Anthropic 牌价估算(写入时按模型单价物化),中转计费可能不同
          </div>
        </>
      )}
    </div>
  )
}
