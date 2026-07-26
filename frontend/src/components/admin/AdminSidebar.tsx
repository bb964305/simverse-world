export type AdminTab =
  | 'overview'
  | 'society'
  | 'economy'
  | 'governance'
  | 'users'
  | 'residents'
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
  label: string
}

const ANALYTICS_ITEMS: NavItem[] = [
  { key: 'overview', icon: '◫', label: '发展总览' },
  { key: 'society', icon: '◎', label: '居民与社会' },
  { key: 'economy', icon: '◇', label: '经济运行' },
  { key: 'governance', icon: '△', label: '治理与事件' },
]

const CONTROL_ITEMS: NavItem[] = [
  { key: 'users', icon: '👥', label: '用户权限' },
  { key: 'residents', icon: '🏠', label: '居民编辑' },
  { key: 'economy_control', icon: '🪙', label: '经济参数' },
  { key: 'events', icon: '📣', label: '事件投放' },
  { key: 'proposals', icon: '🌍', label: '提案审批' },
  { key: 'forge', icon: '🔮', label: '炼化监控' },
  { key: 'llm', icon: '💸', label: '模型用量' },
  { key: 'lab_runs', icon: '🧪', label: '实验楼控制' },
  { key: 'system', icon: '⚙️', label: '系统配置' },
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
  return (
    <header className="admin-sidebar">
      <div className="admin-sidebar__inner">
        <div className="admin-sidebar__brand">
          <span className="admin-sidebar__mark" aria-hidden="true">S</span>
          <span className="admin-sidebar__title">Simverse</span>
          <span className="admin-sidebar__subtitle">小镇观测站</span>
        </div>

        <nav className="admin-sidebar__nav" aria-label="小镇分析导航">
          {ANALYTICS_ITEMS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`admin-nav-item${activeTab === item.key ? ' admin-nav-item--active' : ''}`}
              onClick={() => onTabChange(item.key)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="admin-sidebar__controls">
          <div className="admin-sidebar__foot">
            <span className="admin-live-dot" aria-hidden="true" />
            <span>数据已同步</span>
          </div>
          <button
            type="button"
            className={`admin-control-toggle${controlOpen ? ' admin-control-toggle--open' : ''}`}
            onClick={onControlToggle}
            aria-expanded={controlOpen}
            aria-controls="admin-control-nav"
          >
            <span className="admin-control-toggle__label">控制中心</span>
            <span aria-hidden="true">{controlOpen ? '↑' : '↓'}</span>
          </button>

          {controlOpen && (
            <nav id="admin-control-nav" className="admin-control-nav" aria-label="管理控制导航">
              <div className="admin-control-nav__title">集中管理</div>
              {CONTROL_ITEMS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className={`admin-control-item${activeTab === item.key ? ' admin-control-item--active' : ''}`}
                  onClick={() => onTabChange(item.key)}
                >
                  <span aria-hidden="true">{item.icon}</span>
                  <span>{item.label}</span>
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
