import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getAdminEvents,
  getAdminProposals,
  getTownHallOverview,
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
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    const [townResult, eventResult, proposalResult] = await Promise.allSettled([
      getTownHallOverview(),
      getAdminEvents(token),
      getAdminProposals(token),
    ])
    if (townResult.status === 'fulfilled') setTown(townResult.value)
    if (eventResult.status === 'fulfilled') setEvents(eventResult.value.events)
    if (proposalResult.status === 'fulfilled') setProposals(proposalResult.value.proposals)
    setError([townResult, eventResult, proposalResult].some((result) => result.status === 'rejected'))
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
  }), [events, proposals])

  const proposalStatuses = useMemo(() => {
    const counts = new Map<string, number>()
    for (const proposal of proposals) {
      counts.set(proposal.status, (counts.get(proposal.status) ?? 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [proposals])

  return (
    <div className="admin-analytics-stack">
      <div className="admin-section-heading">
        <div>
          <h2>治理演化</h2>
          <p>观察职位、议案、世界事件和变更提案如何推动小镇演进。</p>
        </div>
        <button type="button" className="admin-ghost-button" onClick={() => { void load() }}>刷新数据</button>
      </div>

      {error && <div className="admin-error">部分治理数据暂时不可用。</div>}

      <div className="admin-metric-grid">
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">现任镇长</div>
          <div className="admin-metric-card__value" style={{ color: '#a995ff', fontSize: 21 }}>
            {town?.mayor?.name || '空缺'}
          </div>
          <div className="admin-metric-card__note">{town?.duties.length ?? 0} 个在任公职</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">开放议案</div>
          <div className="admin-metric-card__value" style={{ color: '#60e6d2' }}>{town?.open_polls.length ?? '—'}</div>
          <div className="admin-metric-card__note">正在形成公共选择</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">生效世界事件</div>
          <div className="admin-metric-card__value" style={{ color: '#77b9ff' }}>{summary.activeEvents}</div>
          <div className="admin-metric-card__note">共记录 {events.length} 个事件</div>
        </div>
        <div className="admin-metric-card">
          <div className="admin-metric-card__label">待审变更</div>
          <div className="admin-metric-card__value" style={{ color: summary.highRisk ? '#ef7d7d' : '#e3b562' }}>
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
                <span style={{ color: '#a995ff' }} aria-hidden="true">◇</span>
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
                <span style={{ color: '#e3b562' }} aria-hidden="true">◆</span>
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
