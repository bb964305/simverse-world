import { useGameStore } from '../../stores/gameStore'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useLocale } from '../../services/locale'

type Tab = 'residents' | 'creator' | 'conversations' | 'transactions' | 'achievements' | 'feed' | 'recap' | 'codex' | 'settings'

const NAV_ITEMS: { key: Tab; icon: string; en: string; zh: string }[] = [
  { key: 'residents', icon: '🏠', en: 'My residents', zh: '我的居民' },
  { key: 'creator', icon: '📊', en: 'Creator', zh: '创作者' },
  { key: 'conversations', icon: '💬', en: 'Conversations', zh: '对话历史' },
  { key: 'transactions', icon: '◎', en: 'SC activity', zh: 'SC 明细' },
  { key: 'achievements', icon: '🏆', en: 'Achievements', zh: '成就' },
  { key: 'feed', icon: '📡', en: 'Activity', zh: '动态' },
  { key: 'recap', icon: '📅', en: 'Weekly recap', zh: '本周回顾' },
  { key: 'codex', icon: '🗺️', en: 'Exploration codex', zh: '探索图鉴' },
  { key: 'settings', icon: '⚙️', en: 'Settings', zh: '设置' },
]

export function ProfileSidebar({ residentCount }: { residentCount: number }) {
  const user = useGameStore((s) => s.user)
  const profileTab = useGameStore((s) => s.profileTab)
  const setProfileTab = useGameStore((s) => s.setProfileTab)
  const isMobile = useIsMobile()
  const locale = useLocale((state) => state.locale)
  const en = locale === 'en'

  // Mobile: the fixed 250px vertical sidebar leaves almost no room for the
  // main content column (see E2E-03 repro — headings/names collapse into a
  // single vertical character each). Replace it with a full-width, compact
  // header + horizontally-scrolling tab strip instead of a side column.
  if (isMobile) {
    return (
      <div data-testid="profile-sidebar-mobile" style={{
        width: '100%',
        background: 'var(--bg-card)',
        borderBottom: '1px solid var(--border)',
        borderRight: 'none',
        padding: '12px 16px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        flexShrink: 0,
      }}>
        {/* User info + balance, condensed into one row */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{
            width: 40, height: 40, background: 'var(--bg-input)', borderRadius: 10,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 20, flexShrink: 0,
          }}>
            👤
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 700, fontSize: 14 }}>{user?.name}</div>
            <div style={{ color: 'var(--text-muted)', fontSize: 11, marginTop: 2 }}>
              {en ? `Created ${residentCount} residents` : `创作了 ${residentCount} 位居民`} · SC {user?.soul_coin_balance ?? 0}
            </div>
          </div>
        </div>

        {/* Navigation as a horizontal tab strip */}
        <div style={{ display: 'flex', gap: 6, overflowX: 'auto' }}>
          {NAV_ITEMS.map((item) => (
            <button key={item.key} onClick={() => setProfileTab(item.key)} style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '8px 12px',
              borderRadius: 8, background: profileTab === item.key ? 'var(--bg-input)' : 'transparent',
              border: 'none', color: profileTab === item.key ? 'var(--text-primary)' : 'var(--text-secondary)',
              fontSize: 13, cursor: 'pointer', whiteSpace: 'nowrap', flexShrink: 0,
              fontWeight: profileTab === item.key ? 600 : 400,
            }}>
              <span style={{ fontSize: 14 }}>{item.icon}</span>{en ? item.en : item.zh}
            </button>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div style={{
      width: 250, minHeight: 'calc(100vh - var(--nav-height))',
      background: 'var(--bg-card)', borderRight: '1px solid var(--border)',
      padding: '24px 16px', display: 'flex', flexDirection: 'column', gap: 24,
    }}>
      {/* User info */}
      <div style={{ textAlign: 'center' }}>
        <div style={{
          width: 72, height: 72, background: 'var(--bg-input)', borderRadius: 12,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 32, margin: '0 auto 12px',
        }}>
          👤
        </div>
        <div style={{ fontWeight: 700, fontSize: 16 }}>{user?.name}</div>
        <div style={{ color: 'var(--text-muted)', fontSize: 12, marginTop: 4 }}>
          {en ? `Created ${residentCount} residents` : `创作了 ${residentCount} 位居民`}
        </div>
      </div>

      {/* Navigation */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {NAV_ITEMS.map((item) => (
          <button key={item.key} onClick={() => setProfileTab(item.key)} style={{
            display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px',
            borderRadius: 8, background: profileTab === item.key ? 'var(--bg-input)' : 'transparent',
            border: 'none', color: profileTab === item.key ? 'var(--text-primary)' : 'var(--text-secondary)',
            fontSize: 14, cursor: 'pointer', textAlign: 'left',
            fontWeight: profileTab === item.key ? 600 : 400,
          }}>
            <span style={{ fontSize: 16 }}>{item.icon}</span>{en ? item.en : item.zh}
          </button>
        ))}
      </div>

      {/* Soul Coin balance */}
      <div style={{ marginTop: 'auto', padding: '12px 14px', background: '#53d76910', borderRadius: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 18 }}>◎</span>
        <div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{en ? 'Soul Credits · offchain' : '灵魂积分 · 链下'}</div>
          <div style={{ fontWeight: 700, color: 'var(--accent-green)', fontSize: 16 }}>{user?.soul_coin_balance ?? 0}</div>
        </div>
      </div>
    </div>
  )
}
