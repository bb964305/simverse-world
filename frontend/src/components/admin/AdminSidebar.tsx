export type AdminTab =
  | 'overview'
  | 'society'
  | 'economy'
  | 'governance'
  | 'users'
  | 'residents'
  | 'hosted_agents'
  | 'economy_control'
  | 'events'
  | 'proposals'
  | 'forge'
  | 'llm'
  | 'lab_runs'
  | 'system'

interface NavItem {
  key: AdminTab
  icon: string
  zh: string
  en: string
}

const ANALYTICS_ITEMS: NavItem[] = [
  { key: 'overview', icon: '◫', zh: '发展总览', en: 'Overview' },
  { key: 'society', icon: '◎', zh: '居民与社会', en: 'Society' },
  { key: 'economy', icon: '◇', zh: '经济运行', en: 'Economy' },
  { key: 'governance', icon: '△', zh: '治理与事件', en: 'Governance' },
]

const CONTROL_ITEMS: NavItem[] = [
  { key: 'users', icon: '👥', zh: '用户权限', en: 'User access' },
  { key: 'residents', icon: '🏠', zh: '居民编辑', en: 'Residents' },
  { key: 'hosted_agents', icon: '🤖', zh: 'Agent 托管', en: 'Hosted Agents' },
  { key: 'economy_control', icon: '🪙', zh: '经济参数', en: 'Economy controls' },
  { key: 'events', icon: '📣', zh: '事件投放', en: 'World events' },
  { key: 'proposals', icon: '🌍', zh: '提案审批', en: 'Proposals' },
  { key: 'forge', icon: '🔮', zh: '炼化监控', en: 'Forge monitor' },
  { key: 'llm', icon: '💸', zh: '模型用量', en: 'Model usage' },
  { key: 'lab_runs', icon: '🧪', zh: '实验楼控制', en: 'Lab control' },
  { key: 'system', icon: '⚙️', zh: '系统配置', en: 'System config' },
]

interface AdminSidebarProps {
  activeTab: AdminTab
  controlOpen: boolean
  onControlToggle: () => void
  onTabChange: (tab: AdminTab) => void
}

export function AdminSidebar({
  activeTab,
  controlOpen,
  onControlToggle,
  onTabChange,
}: AdminSidebarProps) {
  const en = useLocale((state) => state.locale === 'en')
  return (
    <header className="admin-sidebar">
      <div className="admin-sidebar__inner">
        <div className="admin-sidebar__brand">
          <span className="admin-sidebar__mark" aria-hidden="true">S</span>
          <span className="admin-sidebar__title">Simverse</span>
          <span className="admin-sidebar__subtitle">{en ? 'WORLD OBSERVATORY' : '小镇观测站'}</span>
        </div>

        <nav className="admin-sidebar__nav" aria-label={en ? 'World analytics navigation' : '小镇分析导航'}>
          {ANALYTICS_ITEMS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`admin-nav-item${activeTab === item.key ? ' admin-nav-item--active' : ''}`}
              onClick={() => onTabChange(item.key)}
            >
              {en ? item.en : item.zh}
            </button>
          ))}
        </nav>

        <div className="admin-sidebar__controls">
          <div className="admin-sidebar__foot">
            <span className="admin-live-dot" aria-hidden="true" />
            <span>{en ? 'Data synced' : '数据已同步'}</span>
          </div>
          <button
            type="button"
            className={`admin-control-toggle${controlOpen ? ' admin-control-toggle--open' : ''}`}
            onClick={onControlToggle}
            aria-expanded={controlOpen}
            aria-controls="admin-control-nav"
          >
            <span className="admin-control-toggle__label">{en ? 'Control center' : '控制中心'}</span>
            <span aria-hidden="true">{controlOpen ? '↑' : '↓'}</span>
          </button>

          {controlOpen && (
            <nav id="admin-control-nav" className="admin-control-nav" aria-label={en ? 'Administrative control navigation' : '管理控制导航'}>
              <div className="admin-control-nav__title">{en ? 'Operations' : '集中管理'}</div>
              {CONTROL_ITEMS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className={`admin-control-item${activeTab === item.key ? ' admin-control-item--active' : ''}`}
                  onClick={() => onTabChange(item.key)}
                >
                  <span aria-hidden="true">{item.icon}</span>
                  <span>{en ? item.en : item.zh}</span>
                  <b aria-hidden="true">›</b>
                </button>
              ))}
            </nav>
          )}
        </div>
      </div>
    </header>
  )
}
import { useLocale } from '../../services/locale'
