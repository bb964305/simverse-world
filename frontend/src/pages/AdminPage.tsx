import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AdminAccessState } from '../components/admin/AdminAccessState'
import { AdminSidebar, type AdminTab } from '../components/admin/AdminSidebar'
import { DashboardPanel } from '../components/admin/DashboardPanel'
import { SocietyInsightsPanel } from '../components/admin/SocietyInsightsPanel'
import { GovernanceInsightsPanel } from '../components/admin/GovernanceInsightsPanel'
import { UsersPanel } from '../components/admin/UsersPanel'
import { ResidentsPanel } from '../components/admin/ResidentsPanel'
import { HostedAgentsPanel } from '../components/admin/HostedAgentsPanel'
import { ForgeMonitorPanel } from '../components/admin/ForgeMonitorPanel'
import { EconomyControlPanel, EconomyPanel } from '../components/admin/EconomyPanel'
import { EventsPanel } from '../components/admin/EventsPanel'
import { LlmUsagePanel } from '../components/admin/LlmUsagePanel'
import { LabRunsPanel } from '../components/admin/LabRunsPanel'
import { ProposalsPanel } from '../components/admin/ProposalsPanel'
import { SystemConfigPanel } from '../components/admin/SystemConfigPanel'
import { useGameStore } from '../stores/gameStore'
import { useLocale } from '../services/locale'
import '../styles/admin-console.css'

const TAB_TITLES: Record<AdminTab, { zh: string; en: string; eyebrow: string; zhSub: string; enSub: string }> = {
  overview: { zh: '小镇发展总览', en: 'Town overview', eyebrow: 'TOWN PULSE', zhSub: '观察变化方向，快速定位值得关注的趋势。', enSub: 'Track change and locate trends that need attention.' },
  society: { zh: '居民与社会结构', en: 'Residents and society', eyebrow: 'SOCIETY', zhSub: '理解居民构成、关系活力与社区传播。', enSub: 'Understand population, relationships, and community activity.' },
  economy: { zh: '经济运行趋势', en: 'Economy trends', eyebrow: 'ECONOMY', zhSub: '追踪 SC 游戏积分的流通、消费与投资方向。', enSub: 'Track offchain SC gameplay credits, spending, and allocation.' },
  governance: { zh: '治理与事件演化', en: 'Governance and events', eyebrow: 'GOVERNANCE', zhSub: '判断公职、政策、事件与社区共识。', enSub: 'Review offices, policies, world events, and consensus.' },
  users: { zh: '用户权限', en: 'User access', eyebrow: 'CONTROL CENTER', zhSub: '管理账号状态、权限与余额。', enSub: 'Manage account state, permissions, and SC credits.' },
  residents: { zh: '居民编辑', en: 'Resident editor', eyebrow: 'CONTROL CENTER', zhSub: '维护居民档案、区域与运行状态。', enSub: 'Maintain resident profiles, districts, and runtime state.' },
  hosted_agents: { zh: 'Agent 托管', en: 'Hosted Agents', eyebrow: 'CONTROL CENTER', zhSub: '配置常驻 Agent 居民，并观察它们的地图位置、行动与用量。', enSub: 'Configure resident Agents and monitor location, actions, and usage.' },
  economy_control: { zh: '经济参数', en: 'Economy controls', eyebrow: 'CONTROL CENTER', zhSub: '调整会影响小镇经济行为的动态参数。', enSub: 'Adjust runtime parameters that affect the town economy.' },
  events: { zh: '事件投放', en: 'World events', eyebrow: 'CONTROL CENTER', zhSub: '创建和维护小镇世界事件。', enSub: 'Create and maintain live world events.' },
  proposals: { zh: '提案审批', en: 'Proposal review', eyebrow: 'CONTROL CENTER', zhSub: '审查、应用或回退世界变更。', enSub: 'Review, apply, or roll back world changes.' },
  forge: { zh: '炼化监控', en: 'Forge monitor', eyebrow: 'CONTROL CENTER', zhSub: '观察角色炼化任务与服务状态。', enSub: 'Monitor resident Forge jobs and service health.' },
  llm: { zh: '模型用量', en: 'Model usage', eyebrow: 'CONTROL CENTER', zhSub: '分析模型调用、Token 与成本。', enSub: 'Analyze model calls, tokens, and cost.' },
  lab_runs: { zh: '实验楼控制', en: 'Lab control', eyebrow: 'CONTROL CENTER', zhSub: '监督实验任务与运行开关。', enSub: 'Supervise lab runs and runtime gates.' },
  system: { zh: '系统配置', en: 'System configuration', eyebrow: 'CONTROL CENTER', zhSub: '维护运行时配置与功能参数。', enSub: 'Maintain runtime configuration and feature parameters.' },
}

const ANALYTICS_TABS: AdminTab[] = ['overview', 'society', 'economy', 'governance']

export function AdminPage() {
  const en = useLocale((state) => state.locale === 'en')
  const user = useGameStore((s) => s.user)
  const token = useGameStore((s) => s.token)
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState<AdminTab>('overview')
  const [controlOpen, setControlOpen] = useState(false)

  if (!user?.is_admin) {
    return <AdminAccessState kind="forbidden" />
  }

  const selectTab = (tab: AdminTab) => {
    setActiveTab(tab)
    if (!ANALYTICS_TABS.includes(tab)) setControlOpen(true)
  }

  const renderContent = () => {
    if (!token) return null
    switch (activeTab) {
      case 'overview':
        return <DashboardPanel token={token} />
      case 'society':
        return <SocietyInsightsPanel token={token} />
      case 'economy':
        return <EconomyPanel token={token} />
      case 'governance':
        return <GovernanceInsightsPanel token={token} />
      case 'users':
        return <UsersPanel />
      case 'residents':
        return <ResidentsPanel token={token} />
      case 'hosted_agents':
        return <HostedAgentsPanel token={token} />
      case 'economy_control':
        return <EconomyControlPanel token={token} />
      case 'forge':
        return <ForgeMonitorPanel token={token} />
      case 'llm':
        return <LlmUsagePanel token={token} />
      case 'events':
        return <EventsPanel token={token} />
      case 'lab_runs':
        return <LabRunsPanel token={token} />
      case 'proposals':
        return <ProposalsPanel token={token} />
      case 'system':
        return <SystemConfigPanel token={token} />
    }
  }

  const heading = TAB_TITLES[activeTab]
  const analysisMode = ANALYTICS_TABS.includes(activeTab)

  return (
    <div className="admin-console">
      <AdminSidebar
        activeTab={activeTab}
        controlOpen={controlOpen}
        onControlToggle={() => setControlOpen((open) => !open)}
        onTabChange={selectTab}
      />

      <main className="admin-main">
        <header className={`admin-header${analysisMode ? '' : ' admin-header--control'}`}>
          {analysisMode && (
            <div className="admin-header__status">
              <span className="admin-live-dot" aria-hidden="true" />
              {en ? 'World systems healthy' : '小镇运行良好'}
            </div>
          )}
          <div>
            <div className="admin-header__eyebrow">{heading.eyebrow}</div>
            <h1 className="admin-header__title">{en ? heading.en : heading.zh}</h1>
            <p className="admin-header__subtitle">{en ? heading.enSub : heading.zhSub}</p>
          </div>
          <div className="admin-header__actions">
            {!analysisMode && <span className="admin-control-badge">{en ? 'WRITE OPERATIONS' : '写操作区域'}</span>}
            <span className="admin-header__user">{user.name.slice(0, 1).toUpperCase()}</span>
            <button type="button" className="admin-back-button" onClick={() => navigate('/play')}>
              {en ? 'Back to town ↗' : '返回小镇 ↗'}
            </button>
          </div>
        </header>

        <div className={`admin-content${analysisMode ? '' : ' admin-content--control'}`}>
          {renderContent()}
        </div>
      </main>
    </div>
  )
}
