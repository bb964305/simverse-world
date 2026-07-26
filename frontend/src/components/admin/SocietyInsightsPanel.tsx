import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getAdminGossipRecent,
  getAdminResidents,
  getAdminSocialGraph,
  type AdminGossipItem,
  type AdminResident,
  type AdminSocialGraph,
} from '../../services/api'

function reputationOf(resident: AdminResident) {
  const reputation = resident.meta_json?.reputation
  if (!reputation || typeof reputation !== 'object') return 0
  const score = Number((reputation as { score?: unknown }).score)
  return Number.isFinite(score) ? score : 0
}

function Distribution({
  items,
  empty,
}: {
  items: [string, number][]
  empty: string
}) {
  if (items.length === 0) return <div className="admin-empty">{empty}</div>
  return (
    <div className="admin-distribution">
      {items.map(([label, value]) => (
        <div className="admin-distribution__item" key={label}>
          <div className="admin-distribution__value">{value}</div>
          <div className="admin-distribution__label">{label}</div>
        </div>
      ))}
    </div>
  )
}

function RankedResidents({
  residents,
  mode,
}: {
  residents: AdminResident[]
  mode: 'heat' | 'reputation'
}) {
  const ranked = [...residents]
    .sort((a, b) => mode === 'heat' ? b.heat - a.heat : reputationOf(b) - reputationOf(a))
    .slice(0, 6)
  const values = ranked.map((resident) => mode === 'heat' ? resident.heat : reputationOf(resident))
  const max = Math.max(...values.map((value) => Math.abs(value)), 1)

  return (
    <div className="admin-ranked-list">
      {ranked.map((resident) => {
        const value = mode === 'heat' ? resident.heat : reputationOf(resident)
        return (
          <div className="admin-ranked-row" key={resident.id}>
            <div className="admin-ranked-row__copy">
              <div className="admin-ranked-row__label">{resident.name}</div>
              <div className="admin-ranked-row__detail">{resident.district || '未划分区域'} · {resident.status}</div>
            </div>
            <div className="admin-rank-bar" aria-hidden="true">
              <span style={{ width: `${Math.max(4, Math.abs(value) / max * 100)}%` }} />
            </div>
            <span className="admin-status-row__value">{value.toFixed(1)}</span>
          </div>
        )
      })}
    </div>
  )
}

function RumorStream({ rumors }: { rumors: AdminGossipItem[] }) {
  if (rumors.length === 0) return <div className="admin-empty">暂无社会传播样本</div>
  return (
    <div className="admin-event-stream">
      {rumors.slice(0, 6).map((item) => (
        <div className="admin-event-row" key={item.id}>
          <span aria-hidden="true" style={{ color: item.distorted ? '#ef7d7d' : '#60e6d2' }}>
            {item.distorted ? '◆' : '◇'}
          </span>
          <div className="admin-event-row__copy">
            <div className="admin-event-row__label">{item.resident_name || item.resident_slug || '未知居民'}</div>
            <div className="admin-event-row__detail">{item.content}</div>
          </div>
          <span className="admin-status-row__value">{item.hops} 跳</span>
        </div>
      ))}
    </div>
  )
}

export function SocietyInsightsPanel({ token }: { token: string }) {
  const [residents, setResidents] = useState<AdminResident[]>([])
  const [rumors, setRumors] = useState<AdminGossipItem[]>([])
  const [graph, setGraph] = useState<AdminSocialGraph | null>(null)
  const [failedSources, setFailedSources] = useState<string[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    const results = await Promise.allSettled([
      getAdminResidents(token, { page: 1, per_page: 100 }),
      getAdminGossipRecent(token, 50),
      getAdminSocialGraph(token),
    ])
    const [residentResult, rumorResult, graphResult] = results
    if (residentResult.status === 'fulfilled') setResidents(residentResult.value.items)
    if (rumorResult.status === 'fulfilled') setRumors(rumorResult.value.items)
    if (graphResult.status === 'fulfilled') setGraph(graphResult.value)
    const sourceNames = ['居民结构', '社会传播', '关系网络']
    setFailedSources(results.flatMap((result, index) => result.status === 'rejected' ? [sourceNames[index]] : []))
    setLoading(false)
  }, [token])

  useEffect(() => {
    const initial = setTimeout(() => { void load() }, 0)
    const interval = setInterval(() => { void load() }, 60_000)
    return () => {
      clearTimeout(initial)
      clearInterval(interval)
    }
  }, [load])

  const summary = useMemo(() => {
    const npc = residents.filter((resident) => resident.type === 'NPC')
    const players = residents.length - npc.length
    const avgHeat = residents.length
      ? residents.reduce((sum, resident) => sum + resident.heat, 0) / residents.length
      : 0
    const avgReputation = npc.length
      ? npc.reduce((sum, resident) => sum + reputationOf(resident), 0) / npc.length
      : 0
    const distorted = rumors.filter((rumor) => rumor.distorted).length
    return {
      npc,
      players,
      avgHeat,
      avgReputation,
      distortionRate: rumors.length ? distorted / rumors.length * 100 : 0,
      circleCount: graph?.circles.length ?? 0,
      connectedResidents: graph?.nodes.filter((node) => node.circle_id).length ?? 0,
      avgFamiliarity: graph?.edges.length
        ? graph.edges.reduce((sum, edge) => sum + edge.familiarity, 0) / graph.edges.length
        : 0,
    }
  }, [graph, residents, rumors])

  const circleSizes = useMemo(
    () => (graph?.circles ?? [])
      .slice()
      .sort((a, b) => b.activity - a.activity)
      .slice(0, 6)
      .map((circle, index) => [`社交圈 ${index + 1}`, circle.size] as [string, number]),
    [graph],
  )

  const districts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const resident of residents) {
      const label = resident.district || '未划分'
      counts.set(label, (counts.get(label) ?? 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6)
  }, [residents])

  const statuses = useMemo(() => {
    const counts = new Map<string, number>()
    for (const resident of residents) {
      counts.set(resident.status || 'unknown', (counts.get(resident.status || 'unknown') ?? 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6)
  }, [residents])

  return (
    <div className="admin-analytics-stack">
      <div className="admin-section-heading">
        <div>
          <h2>社会脉搏</h2>
          <p>从居民组成、活跃度、公共声誉和信息传播观察社会变化。</p>
        </div>
        <button type="button" className="admin-ghost-button" onClick={() => { void load() }}>
          {loading ? '正在同步…' : '刷新数据'}
        </button>
      </div>

      {failedSources.length > 0 && (
        <div className="admin-error">{failedSources.join('、')}暂时不可用，已保留其他实时数据。</div>
      )}

      <div className="admin-metric-grid">
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">自治居民</div>
          <div className="admin-metric-card__value" style={{ color: '#0071e3' }}>{summary.npc.length}</div>
          <div className="admin-metric-card__note">玩家居民 {summary.players}</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">活跃社交圈</div>
          <div className="admin-metric-card__value" style={{ color: '#34c759' }}>{summary.circleCount}</div>
          <div className="admin-metric-card__note">{summary.connectedResidents} 位居民已形成连接</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">平均活跃热度</div>
          <div className="admin-metric-card__value" style={{ color: '#af52de' }}>{summary.avgHeat.toFixed(1)}</div>
          <div className="admin-metric-card__note">平均熟悉度 {summary.avgFamiliarity.toFixed(2)}</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">流言失真率</div>
          <div className="admin-metric-card__value" style={{ color: summary.distortionRate > 30 ? '#ff3b30' : '#ff9f0a' }}>
            {summary.distortionRate.toFixed(0)}%
          </div>
          <div className="admin-metric-card__note">最近 {rumors.length} 条传播样本</div>
        </div>
      </div>

      <div className="admin-analytics-grid admin-analytics-grid--equal">
        <section className="admin-analytics-card">
          <div className="admin-card-title"><h3>区域人口结构</h3><span>居民数量</span></div>
          <Distribution items={districts} empty="暂无区域数据" />
        </section>
        <section className="admin-analytics-card">
          <div className="admin-card-title"><h3>社交圈结构</h3><span>{graph?.edges.length ?? 0} 条关系</span></div>
          <Distribution items={circleSizes} empty="暂无稳定社交圈" />
        </section>
      </div>

      <div className="admin-analytics-grid admin-analytics-grid--equal">
        <section className="admin-analytics-card">
          <div className="admin-card-title"><h3>高活跃居民</h3><span>Heat</span></div>
          <RankedResidents residents={residents} mode="heat" />
        </section>
        <section className="admin-analytics-card">
          <div className="admin-card-title">
            <h3>公共声誉观察</h3>
            <span>平均 {summary.avgReputation.toFixed(2)}</span>
          </div>
          <RankedResidents residents={summary.npc} mode="reputation" />
        </section>
      </div>

      <section className="admin-analytics-card">
        <div className="admin-card-title"><h3>居民状态结构</h3><span>当前快照</span></div>
        <Distribution items={statuses} empty="暂无状态数据" />
      </section>

      <section className="admin-analytics-card">
        <div className="admin-card-title"><h3>社会传播动态</h3><span>最近流言链样本</span></div>
        <RumorStream rumors={rumors} />
      </section>
    </div>
  )
}
