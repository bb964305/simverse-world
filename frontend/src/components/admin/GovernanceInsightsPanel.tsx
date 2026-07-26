import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getAdminEvents,
  getAdminOffices,
  getAdminPolicies,
  getAdminProposals,
  getTownHallOverview,
  type AdminOffice,
  type AdminPoliciesResponse,
  type AdminWorldEvent,
  type TownHallOverview,
  type WorldProposal,
} from '../../services/api'

function eventStatus(event: AdminWorldEvent) {
  if (event.is_active) return { label: '进行中', color: '#60e6d2' }
  const end = event.ends_at ? new Date(event.ends_at) : null
  if (end && end < new Date()) return { label: '已结束', color: '#77869a' }
  return { label: '待开始', color: '#e3b562' }
}

export function GovernanceInsightsPanel({ token }: { token: string }) {
  const [town, setTown] = useState<TownHallOverview | null>(null)
  const [events, setEvents] = useState<AdminWorldEvent[]>([])
  const [proposals, setProposals] = useState<WorldProposal[]>([])
  const [offices, setOffices] = useState<AdminOffice[]>([])
  const [policies, setPolicies] = useState<AdminPoliciesResponse | null>(null)
  const [failedSources, setFailedSources] = useState<string[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    const results = await Promise.allSettled([
      getTownHallOverview(),
      getAdminEvents(token),
      getAdminProposals(token),
      getAdminOffices(token),
      getAdminPolicies(token),
    ])
    const [townResult, eventResult, proposalResult, officeResult, policyResult] = results
    if (townResult.status === 'fulfilled') setTown(townResult.value)
    if (eventResult.status === 'fulfilled') setEvents(eventResult.value.events)
    if (proposalResult.status === 'fulfilled') setProposals(proposalResult.value.proposals)
    if (officeResult.status === 'fulfilled') setOffices(officeResult.value.offices)
    if (policyResult.status === 'fulfilled') setPolicies(policyResult.value)
    const sourceNames = ['市政概览', '世界事件', '变更提案', '公职体系', '政策分级']
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

  const summary = useMemo(() => ({
    activeEvents: events.filter((event) => event.is_active).length,
    pendingProposals: proposals.filter((proposal) => proposal.status === 'pending').length,
    appliedProposals: proposals.filter((proposal) => proposal.status === 'applied').length,
    highRisk: proposals.filter((proposal) => proposal.risk_level === 'high' && proposal.status === 'pending').length,
    filledOffices: offices.filter((office) => office.holder_slug).length,
  }), [events, offices, proposals])

  const proposalStatuses = useMemo(() => {
    const counts = new Map<string, number>()
    for (const proposal of proposals) {
      counts.set(proposal.status, (counts.get(proposal.status) ?? 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [proposals])

  const policyTiers = useMemo(() => {
    const labels: Record<string, string> = {
      administrative: '行政直批',
      simple_majority: '简单多数',
      absolute_majority: '绝对多数',
      constitutional_core: '宪制核心',
    }
    const counts = new Map<string, number>()
    for (const policy of policies?.policies ?? []) {
      const label = labels[policy.tier] ?? policy.tier
      counts.set(label, (counts.get(label) ?? 0) + 1)
    }
    return [...counts.entries()]
  }, [policies])

  return (
    <div className="admin-analytics-stack">
      <div className="admin-section-heading">
        <div>
          <h2>治理演化</h2>
          <p>观察职位、议案、世界事件和变更提案如何推动小镇演进。</p>
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
          <div className="admin-metric-card__label">现任镇长</div>
          <div className="admin-metric-card__value" style={{ color: '#0071e3', fontSize: 21 }}>
            {town?.mayor?.name || '空缺'}
          </div>
          <div className="admin-metric-card__note">{summary.filledOffices} / {offices.length} 个公职在任</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">开放议案</div>
          <div className="admin-metric-card__value" style={{ color: '#34c759' }}>{town?.open_polls.length ?? '—'}</div>
          <div className="admin-metric-card__note">正在形成公共选择</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">生效世界事件</div>
          <div className="admin-metric-card__value" style={{ color: '#af52de' }}>{summary.activeEvents}</div>
          <div className="admin-metric-card__note">共记录 {events.length} 个事件</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">待审变更</div>
          <div className="admin-metric-card__value" style={{ color: summary.highRisk ? '#ff3b30' : '#ff9f0a' }}>
            {summary.pendingProposals}
          </div>
          <div className="admin-metric-card__note">{summary.highRisk} 个高风险 · {summary.appliedProposals} 个已应用</div>
        </div>
      </div>

      <div className="admin-analytics-grid">
        <section className="admin-analytics-card">
          <div className="admin-card-title"><h3>议案与选举</h3><span>公共决策</span></div>
          <div className="admin-event-stream">
            {(town?.open_polls ?? []).slice(0, 6).map((poll) => (
              <div className="admin-event-row" key={poll.id}>
                <span style={{ color: '#0071e3' }} aria-hidden="true">◇</span>
                <div className="admin-event-row__copy">
                  <div className="admin-event-row__label">{poll.question}</div>
                  <div className="admin-event-row__detail">
                    {poll.options.length} 个选项{poll.closes_at ? ` · 截止 ${poll.closes_at}` : ''}
                  </div>
                </div>
              </div>
            ))}
            {(town?.open_polls.length ?? 0) === 0 && <div className="admin-empty">目前没有开放议案</div>}
            {town?.recent_election && (
              <div className="admin-event-row">
                <span style={{ color: '#ff9f0a' }} aria-hidden="true">◆</span>
                <div className="admin-event-row__copy">
                  <div className="admin-event-row__label">最近选举 · {town.recent_election.winner_name || '未产生结果'}</div>
                  <div className="admin-event-row__detail">{town.recent_election.question}</div>
                </div>
                {town.recent_election.winner_votes != null && (
                  <span className="admin-status-row__value">{town.recent_election.winner_votes} 票</span>
                )}
              </div>
            )}
          </div>
        </section>

        <section className="admin-analytics-card">
          <div className="admin-card-title"><h3>提案落地结构</h3><span>世界变更</span></div>
          <div className="admin-distribution">
            {proposalStatuses.map(([status, value]) => (
              <div className="admin-distribution__item" key={status}>
                <div className="admin-distribution__value">{value}</div>
                <div className="admin-distribution__label">{status}</div>
              </div>
            ))}
            {proposalStatuses.length === 0 && <div className="admin-empty">暂无世界变更提案</div>}
          </div>
        </section>
      </div>

      <div className="admin-analytics-grid admin-analytics-grid--equal">
        <section className="admin-analytics-card">
          <div className="admin-card-title"><h3>公职运行</h3><span>{summary.filledOffices} 个在任</span></div>
          <div className="admin-event-stream">
            {offices.map((office) => (
              <div className="admin-event-row" key={office.office_key}>
                <span style={{ color: office.holder_slug ? '#34c759' : '#aeaeb2' }} aria-hidden="true">●</span>
                <div className="admin-event-row__copy">
                  <div className="admin-event-row__label">{office.office_key}</div>
                  <div className="admin-event-row__detail">
                    {office.institution} · {office.fill_strategy}
                  </div>
                </div>
                <span className="admin-status-row__value">{office.holder_slug || '空缺'}</span>
              </div>
            ))}
            {offices.length === 0 && <div className="admin-empty">暂无公职记录</div>}
          </div>
        </section>

        <section className="admin-analytics-card">
          <div className="admin-card-title">
            <h3>政策分级</h3>
            <span>{policies?.enabled ? `${policies.policies.length} 项政策` : '功能未启用'}</span>
          </div>
          <div className="admin-distribution">
            {policyTiers.map(([tier, value]) => (
              <div className="admin-distribution__item" key={tier}>
                <div className="admin-distribution__value">{value}</div>
                <div className="admin-distribution__label">{tier}</div>
              </div>
            ))}
            {policyTiers.length === 0 && (
              <div className="admin-empty">{policies?.enabled ? '暂无政策记录' : '政策存储尚未启用'}</div>
            )}
          </div>
        </section>
      </div>

      <section className="admin-analytics-card">
        <div className="admin-card-title"><h3>世界事件时间线</h3><span>最近记录</span></div>
        <div className="admin-event-stream">
          {events.slice(0, 8).map((event) => {
            const status = eventStatus(event)
            return (
              <div className="admin-event-row" key={event.id}>
                <span style={{ color: status.color }} aria-hidden="true">●</span>
                <div className="admin-event-row__copy">
                  <div className="admin-event-row__label">{event.title}</div>
                  <div className="admin-event-row__detail">{event.description || event.type}</div>
                </div>
                <span className="admin-status-row__value" style={{ color: status.color }}>{status.label}</span>
              </div>
            )
          })}
          {events.length === 0 && <div className="admin-empty">暂无世界事件记录</div>}
        </div>
      </section>
    </div>
  )
}
