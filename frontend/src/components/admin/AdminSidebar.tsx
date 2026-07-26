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
  description?: string
}

const ANALYTICS_ITEMS: NavItem[] = [
  { key: 'overview', icon: '◫', label: '发展总览', description: '关键趋势与异常' },
  { key: 'society', icon: '◎', label: '居民与社会', description: '结构、活力与声誉' },
  { key: 'economy', icon: '◇', label: '经济运行', description: '流通、投资与消费' },
  { key: 'governance', icon: '△', label: '治理与事件', description: '政策、议案与演化' },
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

function NavButton({
  item,
  active,
  onClick,
  compact = false,
}: {
  item: NavItem
  active: boolean
  onClick: () => void
  compact?: boolean
}) {
  return (
    <button
      type="button"
      className={`admin-nav-item${active ? ' admin-nav-item--active' : ''}${compact ? ' admin-nav-item--compact' : ''}`}
      onClick={onClick}
    >
      <span className="admin-nav-item__icon" aria-hidden="true">{item.icon}</span>
      <span className="admin-nav-item__copy">
        <span className="admin-nav-item__label">{item.label}</span>
        {!compact && item.description && (
          <span className="admin-nav-item__description">{item.description}</span>
        )}
      </span>
    </button>
  )
}

export function AdminSidebar({
  activeTab,
  controlOpen,
  onControlToggle,
  onTabChange,
}: AdminSidebarProps) {
  return (
    <aside className="admin-sidebar">
      <div className="admin-sidebar__brand">
        <div className="admin-sidebar__eyebrow">SIMVERSE WORLD</div>
        <div className="admin-sidebar__title">小镇观测站</div>
        <div className="admin-sidebar__subtitle">Town Observatory</div>
      </div>

      <nav className="admin-sidebar__nav" aria-label="小镇分析导航">
        <div className="admin-sidebar__section-label">分析视图</div>
        {ANALYTICS_ITEMS.map((item) => (
          <NavButton
            key={item.key}
            item={item}
            active={activeTab === item.key}
            onClick={() => onTabChange(item.key)}
          />
        ))}
      </nav>

      <div className="admin-sidebar__controls">
        <button
          type="button"
          className={`admin-control-toggle${controlOpen ? ' admin-control-toggle--open' : ''}`}
          onClick={onControlToggle}
          aria-expanded={controlOpen}
          aria-controls="admin-control-nav"
        >
          <span>
            <span className="admin-control-toggle__label">控制中心</span>
            <span className="admin-control-toggle__hint">写操作集中区域</span>
          </span>
          <span aria-hidden="true">{controlOpen ? '−' : '+'}</span>
        </button>

        {controlOpen && (
          <nav id="admin-control-nav" className="admin-control-nav" aria-label="管理控制导航">
            {CONTROL_ITEMS.map((item) => (
              <NavButton
                key={item.key}
                item={item}
                compact
                active={activeTab === item.key}
                onClick={() => onTabChange(item.key)}
              />
            ))}
          </nav>
        )}
      </div>

      <div className="admin-sidebar__foot">
        <span className="admin-live-dot" aria-hidden="true" />
        <span>数据自动刷新</span>
      </div>
    </aside>
  )
}
