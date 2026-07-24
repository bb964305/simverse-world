import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useGameStore } from '../stores/gameStore'
import { SearchDropdown } from './SearchDropdown'
import { NotificationDrawer } from './NotificationDrawer'
import { DigestModal } from './DigestModal'
import { CommissionModal } from './CommissionModal'
import { BulletinBoard } from './BulletinBoard'
import { ExperimentPanel } from './ExperimentPanel'
import { ShopModal } from './ShopModal'
import { bridge } from '../game/phaserBridge'
import { disconnectWS, onWSMessage } from '../services/ws'
import { getNotifications, getDailyQuest, getActiveEvents, getMe } from '../services/api'
import type { DailyQuestResponse, ActiveEventData } from '../services/api'
import '../styles/game-ui.css'

// Streak reward ladder (D3): SC amounts for each day of the 7-day cycle.
const STREAK_LADDER = [10, 15, 20, 25, 30, 40, 50]

// Keep dismissals across TopNav remounts without leaking them between accounts.
const dismissedEventsByUser = new Map<string, Set<string>>()

function getDismissedEvents(userId: string | undefined): Set<string> {
  const key = userId ?? 'anonymous'
  const existing = dismissedEventsByUser.get(key)
  if (existing) return existing
  const created = new Set<string>()
  dismissedEventsByUser.set(key, created)
  return created
}

type NavPopover = 'streak' | 'notifications' | 'account' | 'menu' | null
type NavModal = 'digest' | 'commission' | 'shop' | null

function useClock() {
  const [time, setTime] = useState(() => new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }))
  useEffect(() => {
    const id = setInterval(() => {
      setTime(new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }))
    }, 30_000)
    return () => clearInterval(id)
  }, [])
  return time
}

export function TopNav() {
  const user = useGameStore((s) => s.user)
  const logout = useGameStore((s) => s.logout)
  const balance = user?.soul_coin_balance ?? 0
  const navigate = useNavigate()
  const [activePopover, setActivePopover] = useState<NavPopover>(null)
  const [activeModal, setActiveModal] = useState<NavModal>(null)
  const avatarRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const clock = useClock()
  const unreadCount = useGameStore((s) => s.unreadCount)
  const setNotifications = useGameStore((s) => s.setNotifications)
  const notifRef = useRef<HTMLDivElement>(null)
  const digestUnread = useGameStore((s) => s.digestUnread)
  const setDigestUnread = useGameStore((s) => s.setDigestUnread)

  // Login streak + daily quest popup (D3)
  const [dailyData, setDailyData] = useState<DailyQuestResponse | null>(null)
  const streakRef = useRef<HTMLDivElement>(null)
  const streakBtnRef = useRef<HTMLButtonElement>(null)
  const [streakPopupPos, setStreakPopupPos] = useState<{ top: number; right: number }>({ top: 54, right: 16 })

  // Active world events banner (A2)
  const [events, setEvents] = useState<ActiveEventData[]>([])
  const [eventIdx, setEventIdx] = useState(0)
  const [bannerVisible, setBannerVisible] = useState(true)
  const dismissedEvents = getDismissedEvents(user?.id)
  const streakOpen = activePopover === 'streak'
  const notifOpen = activePopover === 'notifications'
  const dropdownOpen = activePopover === 'account'
  const menuOpen = activePopover === 'menu'

  const refreshDailyQuest = useCallback(() => {
    getDailyQuest().then(setDailyData).catch(() => {})
  }, [])

  // Seed the unread badge on mount (notifications produced while offline).
  useEffect(() => {
    getNotifications()
      .then((r) => setNotifications(r.notifications, r.unread_count))
      .catch(() => {})
  }, [setNotifications])

  // Refresh the balance from the server on mount — the persisted store copy
  // goes stale whenever coins move outside a coin_update WS frame (stakes,
  // purchases made on other pages, or plain reloads).
  const updateBalance = useGameStore((s) => s.updateBalance)
  // Lab entry is deploy-gated (LAB_ENABLED): hidden until /users/me confirms
  // the feature is on, so a disabled deploy never shows a dead entry (P2 fix).
  const [labEnabled, setLabEnabled] = useState(false)
  useEffect(() => {
    getMe()
      .then((me) => {
        updateBalance(me.soul_coin_balance)
        setLabEnabled(me.lab_enabled ?? false)
      })
      .catch(() => {})
  }, [updateBalance])

  // D3: fetch quest/streak on mount; A2: fetch active events (60s server cache).
  useEffect(() => {
    refreshDailyQuest()
    getActiveEvents()
      .then((r) => setEvents(r.events.filter((e) => e.type !== 'season' && !dismissedEvents.has(e.id))))
      .catch(() => {})
  }, [dismissedEvents, refreshDailyQuest])

  // Refetch when the popup opens — streak/quest status may have changed.
  useEffect(() => {
    if (streakOpen) refreshDailyQuest()
  }, [streakOpen, refreshDailyQuest])

  // WS: daily_reward bumps the streak (D3); world_event flips the banner (A2).
  useEffect(() => {
    return onWSMessage((data) => {
      if (data.type === 'daily_reward') refreshDailyQuest()
      if (data.type === 'world_event' && data.event && typeof data.event === 'object') {
        const ev = data.event as ActiveEventData
        if (data.phase === 'start') {
          if (ev.type !== 'season' && !dismissedEvents.has(ev.id)) {
            setEvents((prev) => (prev.some((e) => e.id === ev.id) ? prev : [...prev, ev]))
          }
        } else if (data.phase === 'end') {
          setEvents((prev) => prev.filter((e) => e.id !== ev.id))
        }
      }
    })
  }, [dismissedEvents, refreshDailyQuest])

  // A2: cycle through multiple events every 8s with a short fade. The render
  // indexes with `eventIdx % events.length`, so no sync reset is needed when
  // the list shrinks; the fade timeout is left to fire (it only restores
  // visibility, a harmless no-op after unmount).
  useEffect(() => {
    if (events.length <= 1) return
    const id = setInterval(() => {
      setBannerVisible(false)
      setTimeout(() => {
        setEventIdx((i) => i + 1)
        setBannerVisible(true)
      }, 300)
    }, 8000)
    return () => clearInterval(id)
  }, [events.length])

  // Only one nav popover can own focus at a time. Outside click and Escape use
  // the active popover's trigger/container as the boundary.
  useEffect(() => {
    if (!activePopover) return
    const activeRef = activePopover === 'streak' ? streakRef
      : activePopover === 'notifications' ? notifRef
        : activePopover === 'account' ? avatarRef
          : menuRef
    const handler = (e: MouseEvent) => {
      if (activeRef.current && !activeRef.current.contains(e.target as Node)) setActivePopover(null)
    }
    const keyHandler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setActivePopover(null)
    }
    document.addEventListener('mousedown', handler)
    document.addEventListener('keydown', keyHandler)
    return () => {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('keydown', keyHandler)
    }
  }, [activePopover])

  const toggleStreak = () => {
    const rect = streakBtnRef.current?.getBoundingClientRect()
    if (rect) {
      setStreakPopupPos({ top: rect.bottom + 6, right: Math.max(8, window.innerWidth - rect.right) })
    }
    setActivePopover((current) => current === 'streak' ? null : 'streak')
  }

  const dismissEvent = (id: string) => {
    dismissedEvents.add(id)
    setEvents((prev) => prev.filter((e) => e.id !== id))
  }

  const loginStreak = dailyData?.login_streak ?? 0
  const streakIdx = loginStreak > 0 ? (loginStreak - 1) % 7 : -1
  const quest = dailyData?.quest ?? null
  const currentEvent = events.length > 0 ? events[eventIdx % events.length] : null

  useEffect(() => {
    document.documentElement.style.setProperty('--game-event-height', currentEvent ? '30px' : '0px')
    return () => document.documentElement.style.setProperty('--game-event-height', '0px')
  }, [currentEvent])

  useEffect(() => {
    if (!activeModal) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setActiveModal(null)
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [activeModal])

  // Bridge-opened panels and nav-owned dialogs share one exclusive overlay lane.
  useEffect(() => {
    const closeLocalLayers = () => {
      setActiveModal(null)
      setActivePopover(null)
    }
    const unsubBulletin = bridge.on('bulletin:open', closeLocalLayers)
    const unsubExperiment = bridge.on('experiment:open', closeLocalLayers)
    return () => { unsubBulletin(); unsubExperiment() }
  }, [])

  const closeBridgePanels = () => {
    bridge.emit('bulletin:close')
    bridge.emit('experiment:close')
  }

  const openModal = (modal: Exclude<NavModal, null>) => {
    closeBridgePanels()
    setActivePopover(null)
    setActiveModal(modal)
  }

  const openBridgePanel = (panel: 'bulletin' | 'experiment') => {
    setActiveModal(null)
    setActivePopover(null)
    bridge.emit(panel === 'bulletin' ? 'experiment:close' : 'bulletin:close')
    bridge.emit(`${panel}:open`)
  }

  const navigateTo = (path: string) => {
    setActivePopover(null)
    navigate(path)
  }

  const handleLogout = () => {
    disconnectWS()
    logout()
    navigate('/login')
  }

  return (<>
    <nav className="game-topnav" aria-label="游戏主导航">
      <div className="game-topnav__left">
        <button className="game-topnav__brand" onClick={() => navigateTo('/')} aria-label="返回 Simverse World">
          <span className="game-topnav__brand-mark" aria-hidden="true">🏙️</span>
          <span className="game-topnav__brand-word">Simverse World</span>
        </button>
        <div className="game-topnav__links">
          <button onClick={() => navigateTo('/forge')} className="game-nav-link game-nav-link--primary">＋ 炼化居民</button>
          <button onClick={() => openBridgePanel('bulletin')} className="game-nav-link game-nav-link--gold">📋 公告板</button>
          <button onClick={() => navigateTo('/seasons')} className="game-nav-link game-nav-link--gold">🏆 赛季</button>
          <button onClick={() => navigateTo('/debates')} className="game-nav-link game-nav-link--violet">⚔️ 辩论</button>
          <button onClick={() => openModal('shop')} className="game-nav-link game-nav-link--pink">🛒 商店</button>
          <button onClick={() => openModal('commission')} className="game-nav-link game-nav-link--green">🗒️ 委托</button>
          {labEnabled && (
            <button onClick={() => openBridgePanel('experiment')} className="game-nav-link game-nav-link--teal">🧪 实验楼</button>
          )}
          {user?.is_admin && (
            <button onClick={() => navigateTo('/admin')} className="game-nav-link game-nav-link--danger">🔐 管理</button>
          )}
        </div>
        <div ref={menuRef} className="game-topnav__control">
          <button
            className="game-topnav__menu-button"
            onClick={() => setActivePopover((current) => current === 'menu' ? null : 'menu')}
            aria-label={menuOpen ? '关闭世界菜单' : '打开世界菜单'}
            aria-expanded={menuOpen}
            aria-controls="game-world-menu"
          >
            <span aria-hidden="true">☰</span><span>世界</span>
          </button>
          {menuOpen && (
            <div id="game-world-menu" className="game-nav-menu" role="menu">
              <div className="game-nav-menu__search"><SearchDropdown /></div>
              <button onClick={() => navigateTo('/forge')} className="game-nav-link game-nav-link--primary" role="menuitem">＋ 炼化居民</button>
              <button onClick={() => openBridgePanel('bulletin')} className="game-nav-link game-nav-link--gold" role="menuitem">📋 公告板</button>
              <button onClick={() => navigateTo('/seasons')} className="game-nav-link game-nav-link--gold" role="menuitem">🏆 赛季</button>
              <button onClick={() => navigateTo('/debates')} className="game-nav-link game-nav-link--violet" role="menuitem">⚔️ 辩论</button>
              <button onClick={() => openModal('shop')} className="game-nav-link game-nav-link--pink" role="menuitem">🛒 商店</button>
              <button onClick={() => openModal('commission')} className="game-nav-link game-nav-link--green" role="menuitem">🗒️ 委托</button>
              {labEnabled && (
                <button onClick={() => openBridgePanel('experiment')} className="game-nav-link game-nav-link--teal" role="menuitem">🧪 实验楼</button>
              )}
              <button onClick={() => { setDigestUnread(false); openModal('digest') }} className="game-nav-link" role="menuitem">📰 村落日报</button>
              {user?.is_admin && (
                <button onClick={() => navigateTo('/admin')} className="game-nav-link game-nav-link--danger" role="menuitem">🔐 管理</button>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="game-topnav__search"><SearchDropdown /></div>
      <div className="game-topnav__actions">
        <span className="game-topnav__status game-topnav__clock" style={{ fontVariantNumeric: 'tabular-nums' }}>🕐 {clock}</span>
        <span className="game-topnav__status game-topnav__status--coin">🪙 {balance}</span>
        {/* Login streak + daily quest (D3) */}
        <div ref={streakRef} className="game-topnav__control game-topnav__streak">
          <button
            ref={streakBtnRef}
            onClick={toggleStreak}
            title="连续登录"
            className="game-topnav__status"
            aria-expanded={streakOpen}
          >
            🔥{loginStreak}
          </button>
          {streakOpen && (
            <div
              className="game-nav-popover game-nav-popover--streak"
              style={{ top: streakPopupPos.top, right: streakPopupPos.right }}
              role="dialog"
              aria-label="连续登录与今日话题"
            >
              <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-primary)' }}>
                连续登录 {loginStreak} 天 🔥
              </div>
              {/* 7-day reward ladder — current day highlighted */}
              <div style={{ display: 'flex', gap: 4, marginTop: 12 }}>
                {STREAK_LADDER.map((sc, i) => (
                  <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
                    <div style={{
                      width: 10, height: 10, borderRadius: '50%', boxSizing: 'border-box',
                      background: i === streakIdx ? 'var(--accent-red)'
                        : i < streakIdx ? 'rgba(233,69,96,0.35)' : 'var(--bg-input)',
                      border: i === streakIdx ? '1px solid var(--accent-red)' : '1px solid var(--border)',
                    }} />
                    <span style={{
                      fontSize: 9,
                      color: i === streakIdx ? 'var(--accent-red)' : 'var(--text-muted)',
                      fontWeight: i === streakIdx ? 700 : 400,
                    }}>{sc}</span>
                  </div>
                ))}
              </div>
              {/* 今日话题卡片 */}
              <div style={{
                marginTop: 12, background: 'var(--bg-input)', borderRadius: 8, padding: '10px 12px',
              }}>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>今日话题</div>
                {quest ? (
                  <>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
                        {quest.quest.resident_name}
                      </span>
                      <span style={{
                        marginLeft: 'auto', fontSize: 10, fontWeight: 600,
                        padding: '1px 8px', borderRadius: 8,
                        ...(quest.status === 'done'
                          ? { background: 'rgba(83,215,105,0.15)', color: 'var(--accent-green)' }
                          : { background: 'rgba(14,165,233,0.15)', color: 'var(--accent-blue)' }),
                      }}>
                        {quest.status === 'done' ? '✅ 已完成' : '进行中'}
                      </span>
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4, lineHeight: 1.5 }}>
                      {quest.quest.topic}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
                      与TA聊满 {quest.quest.min_turns} 轮可得 {quest.reward_sc}🪙
                    </div>
                  </>
                ) : (
                  <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>今日暂无话题</div>
                )}
              </div>
            </div>
          )}
        </div>
        <button
          onClick={() => { setDigestUnread(false); openModal('digest') }}
          title="村落日报"
          className="game-topnav__icon-button game-topnav__digest"
        >
          📰
          {digestUnread && (
            <span className="game-topnav__badge game-topnav__badge--dot" />
          )}
        </button>
        <div ref={notifRef} className="game-topnav__control">
          <button
            onClick={() => setActivePopover((current) => current === 'notifications' ? null : 'notifications')}
            title="通知"
            className="game-topnav__icon-button"
            aria-expanded={notifOpen}
          >
            🔔
            {unreadCount > 0 && (
              <span className="game-topnav__badge">{unreadCount > 99 ? '99+' : unreadCount}</span>
            )}
          </button>
          {notifOpen && <NotificationDrawer onClose={() => setActivePopover(null)} />}
        </div>
        <div ref={avatarRef} className="game-topnav__control">
          <button
            onClick={() => setActivePopover((current) => current === 'account' ? null : 'account')}
            className="game-topnav__avatar"
            aria-label="账号菜单"
            aria-expanded={dropdownOpen}
          >
            {user?.name?.[0]?.toUpperCase() || '?'}
          </button>
          {dropdownOpen && (
            <div className="game-nav-popover game-nav-popover--account" role="menu">
              <div style={{
                padding: '10px 14px', fontSize: 13, fontWeight: 600,
                color: 'var(--text-primary)', borderBottom: '1px solid var(--border)',
              }}>
                {user?.name ?? '用户'}
              </div>
              <button
                onClick={() => navigateTo('/profile')}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  padding: '9px 14px', fontSize: 13,
                  color: 'var(--text-primary)', background: 'none', border: 'none',
                  cursor: 'pointer',
                }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = 'var(--bg-input)' }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = 'none' }}
              >
                👤 个人主页
              </button>
              <button
                onClick={() => navigateTo('/capsules')}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  padding: '9px 14px', fontSize: 13,
                  color: 'var(--text-primary)', background: 'none', border: 'none',
                  cursor: 'pointer',
                }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = 'var(--bg-input)' }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = 'none' }}
              >
                💌 时间胶囊
              </button>
              <button
                onClick={handleLogout}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  padding: '9px 14px', fontSize: 13,
                  color: '#ff6b6b', background: 'none', border: 'none',
                  cursor: 'pointer', borderTop: '1px solid var(--border)',
                }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = 'var(--bg-input)' }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = 'none' }}
              >
                🚪 退出登录
              </button>
            </div>
          )}
        </div>
      </div>
    </nav>
    {activeModal === 'digest' && <DigestModal onClose={() => setActiveModal(null)} />}
    {activeModal === 'commission' && <CommissionModal onClose={() => setActiveModal(null)} />}
    {activeModal === 'shop' && <ShopModal onClose={() => setActiveModal(null)} />}
    {/* Viewport overlays stay outside nav so backdrop-filter cannot establish
        a 48px fixed-position containing block around them. */}
    <BulletinBoard />
    <ExperimentPanel />
    {/* The shared event-height variable moves every game HUD surface below it. */}
    {currentEvent && (
      <div className="game-world-event" style={{ opacity: bannerVisible ? 1 : 0, transition: 'opacity 0.3s ease' }} role="status">
        <span>📣</span>
        <span className="game-world-event__copy">
          <span style={{ fontWeight: 600 }}>{currentEvent.title}</span>
          <span className="game-world-event__description" style={{ color: '#d2c5c4' }}> · {currentEvent.description.slice(0, 80)}</span>
        </span>
        {events.length > 1 && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontVariantNumeric: 'tabular-nums' }}>
            {(eventIdx % events.length) + 1}/{events.length}
          </span>
        )}
        <button
          onClick={() => dismissEvent(currentEvent.id)}
          aria-label="关闭世界事件"
          className="game-dialog-close"
        >✕</button>
      </div>
    )}
  </>)
}
