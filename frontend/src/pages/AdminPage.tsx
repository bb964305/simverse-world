import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { AdminSidebar, type AdminTab } from '../components/admin/AdminSidebar'
import { DashboardPanel } from '../components/admin/DashboardPanel'
import { SocietyInsightsPanel } from '../components/admin/SocietyInsightsPanel'
import { GovernanceInsightsPanel } from '../components/admin/GovernanceInsightsPanel'
import { UsersPanel } from '../components/admin/UsersPanel'
import { ResidentsPanel } from '../components/admin/ResidentsPanel'
import { ForgeMonitorPanel } from '../components/admin/ForgeMonitorPanel'
import { EconomyControlPanel, EconomyPanel } from '../components/admin/EconomyPanel'
import { EventsPanel } from '../components/admin/EventsPanel'
import { LlmUsagePanel } from '../components/admin/LlmUsagePanel'
import { LabRunsPanel } from '../components/admin/LabRunsPanel'
import { ProposalsPanel } from '../components/admin/ProposalsPanel'
import { SystemConfigPanel } from '../components/admin/SystemConfigPanel'
import { useGameStore } from '../stores/gameStore'
import '../styles/admin-console.css'

const TAB_TITLES: Record<AdminTab, { title: string; eyebrow: string }> = {
  overview: { title: '小镇发展总览', eyebrow: 'TOWN PULSE' },
  society: { title: '居民与社会结构', eyebrow: 'SOCIETY' },
  economy: { title: '经济运行趋势', eyebrow: 'ECONOMY' },
  governance: { title: '治理与事件演化', eyebrow: 'GOVERNANCE' },
  users: { title: '用户权限', eyebrow: 'CONTROL CENTER' },
  residents: { title: '居民编辑', eyebrow: 'CONTROL CENTER' },
  economy_control: { title: '经济参数', eyebrow: 'CONTROL CENTER' },
  events: { title: '事件投放', eyebrow: 'CONTROL CENTER' },
  proposals: { title: '提案审批', eyebrow: 'CONTROL CENTER' },
  forge: { title: '炼化监控', eyebrow: 'CONTROL CENTER' },
  llm: { title: '模型用量', eyebrow: 'CONTROL CENTER' },
  lab_runs: { title: '实验楼控制', eyebrow: 'CONTROL CENTER' },
  system: { title: '系统配置', eyebrow: 'CONTROL CENTER' },
}

const ANALYTICS_TABS: AdminTab[] = ['overview', 'society', 'economy', 'governance']

export function AdminPage() {
  const user = useGameStore((s) => s.user)
  const token = useGameStore((s) => s.token)
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState<AdminTab>('overview')
  const [controlOpen, setControlOpen] = useState(false)

  if (!user?.is_admin) {
    return <Navigate to="/play" replace />
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
        <header className="admin-header">
          <div>
            <div className="admin-header__eyebrow">{heading.eyebrow}</div>
            <h1 className="admin-header__title">{heading.title}</h1>
          </div>
          <div className="admin-header__actions">
            {!analysisMode && <span className="admin-control-badge">写操作区域</span>}
            <span className="admin-header__user">{user.name}</span>
            <button type="button" className="admin-back-button" onClick={() => navigate('/play')}>
              返回小镇
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
