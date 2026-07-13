import { useEffect, useState } from 'react'
import {
  getAdminGossipRecent,
  getAdminRumorChain,
  type AdminGossipItem,
  type AdminRumorChainNode,
} from '../../services/api'

// ─── Shared sub-components (same style as EconomyPanel) ──────────

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

function HopBadge({ hops }: { hops: number }) {
  return (
    <span style={{
      fontSize: 10, fontWeight: 600, padding: '1px 6px', borderRadius: 8,
      background: hops === 0 ? '#53d76922' : 'var(--bg-input)',
      border: '1px solid var(--border)',
      color: hops === 0 ? '#53d769' : 'var(--text-muted)',
      whiteSpace: 'nowrap',
    }}>
      {hops === 0 ? '源头' : `第 ${hops} 跳`}
    </span>
  )
}

function DistortedBadge() {
  return (
    <span style={{
      fontSize: 10, fontWeight: 600, padding: '1px 6px', borderRadius: 8,
      background: '#ff6b6b18', border: '1px solid #ff6b6b55', color: '#ff6b6b',
      whiteSpace: 'nowrap',
    }}>
      ⚠️ 失真
    </span>
  )
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

// ─── Chain tree (indented by hops) ───────────────────────────────

function ChainTree({ chain }: { chain: AdminRumorChainNode[] }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 8 }}>
      {chain.map((node) => (
        <div
          key={node.id}
          style={{
            marginLeft: node.hops * 22,
            padding: '6px 10px',
            background: 'var(--bg-card)',
            border: node.distorted ? '1px solid #ff6b6b55' : '1px solid var(--border)',
            borderRadius: 6,
            display: 'flex', alignItems: 'center', gap: 8,
          }}
        >
          <span style={{ fontSize: 11, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
            {node.hops === 0 ? '●' : '└─'}
          </span>
          <HopBadge hops={node.hops} />
          {node.distorted && <DistortedBadge />}
          <span style={{ fontSize: 12, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
            {node.resident_name ?? node.resident_id}
          </span>
          <span style={{ flex: 1, fontSize: 12, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={node.content}>
            {node.content}
          </span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
            重要度 {node.importance != null ? node.importance.toFixed(2) : '—'}
          </span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
            {fmtTime(node.created_at)}
          </span>
        </div>
      ))}
    </div>
  )
}

// ─── Main panel ──────────────────────────────────────────────────

interface RumorChainPanelProps {
  token: string
}

export function RumorChainPanel({ token }: RumorChainPanelProps) {
  const [items, setItems] = useState<AdminGossipItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [chains, setChains] = useState<Record<string, AdminRumorChainNode[]>>({})
  const [chainLoadingId, setChainLoadingId] = useState<string | null>(null)
  const [chainError, setChainError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      setLoading(true)
      setError(null)
      try {
        const data = await getAdminGossipRecent(token, 50)
        setItems(data.items)
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : '加载失败')
      } finally {
        setLoading(false)
      }
    })()
  }, [token])

  const handleToggle = async (item: AdminGossipItem) => {
    if (expandedId === item.id) {
      setExpandedId(null)
      return
    }
    setExpandedId(item.id)
    setChainError(null)
    if (chains[item.id]) return // already fetched — chains are static per memory
    setChainLoadingId(item.id)
    try {
      const data = await getAdminRumorChain(token, item.id)
      setChains((prev) => ({ ...prev, [item.id]: data.chain }))
    } catch (err: unknown) {
      setChainError(err instanceof Error ? err.message : '加载传播链失败')
    } finally {
      setChainLoadingId(null)
    }
  }

  return (
    <div style={{ maxWidth: 900 }}>
      <h1 style={{ fontSize: 20, fontWeight: 700, marginBottom: 24, color: 'var(--text-primary)' }}>
        谣言链（E3）
      </h1>

      <SectionCard>
        <SectionHeader icon="🗣️" title="最近 gossip 记忆（点击展开传播链）" />

        {loading ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '12px 0' }}>加载中...</div>
        ) : error ? (
          <div style={{ color: '#ff6b6b', fontSize: 13, padding: '12px 0' }}>{error}</div>
        ) : items.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: '24px 0' }}>
            还没有 gossip 记忆 — 居民间闲聊达到一定次数后会自然产生。
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {items.map((item) => {
              const expanded = expandedId === item.id
              return (
                <div key={item.id}>
                  <button
                    onClick={() => void handleToggle(item)}
                    style={{
                      width: '100%',
                      display: 'flex', alignItems: 'center', gap: 8,
                      padding: '8px 12px',
                      background: expanded ? 'var(--bg-card)' : 'var(--bg-input)',
                      border: '1px solid var(--border)',
                      borderRadius: 6,
                      cursor: 'pointer',
                      textAlign: 'left',
                    }}
                  >
                    <span style={{ fontSize: 11, color: 'var(--text-muted)', width: 12 }}>
                      {expanded ? '▾' : '▸'}
                    </span>
                    <HopBadge hops={item.hops} />
                    {item.distorted && <DistortedBadge />}
                    <span style={{ fontSize: 12, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
                      {item.resident_name ?? item.resident_id}
                    </span>
                    <span style={{ flex: 1, fontSize: 12, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={item.content}>
                      {item.content}
                    </span>
                    <span style={{ fontSize: 10, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
                      重要度 {item.importance.toFixed(2)}
                    </span>
                    <span style={{ fontSize: 10, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
                      {fmtTime(item.created_at)}
                    </span>
                  </button>

                  {expanded && (
                    <div style={{ padding: '4px 12px 10px 24px' }}>
                      {chainLoadingId === item.id ? (
                        <div style={{ color: 'var(--text-muted)', fontSize: 12, padding: '8px 0' }}>加载传播链...</div>
                      ) : chainError ? (
                        <div style={{ color: '#ff6b6b', fontSize: 12, padding: '8px 0' }}>{chainError}</div>
                      ) : chains[item.id] ? (
                        <ChainTree chain={chains[item.id]} />
                      ) : null}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 12 }}>
          传播链按 origin_memory_id 回溯：源头（hops 0）→ 逐跳转述；每跳重要度 ×0.8，失真概率随跳数上升，4 跳后终止。
        </div>
      </SectionCard>
    </div>
  )
}
