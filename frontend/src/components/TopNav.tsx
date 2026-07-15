import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useGameStore } from '../stores/gameStore'
import { SearchDropdown } from './SearchDropdown'
import { NotificationDrawer } from './NotificationDrawer'
import { DigestModal } from './DigestModal'
import { CommissionModal } from './CommissionModal'
import { BulletinBoard } from './BulletinBoard'
import { ShopModal } from './ShopModal'
import { bridge } from '../game/phaserBridge'
import { disconnectWS, onWSMessage } from '../services/ws'
import { getNotifications, getDailyQuest, getActiveEvents, getMe } from '../services/api'
import type { DailyQuestResponse, ActiveEventData } from '../services/api'

// Streak reward ladder (D3): SC amounts for each day of the 7-day cycle.
const STREAK_LADDER = [10, 15, 20, 25, 30, 40, 50]

// World-event banner (A2): dismissals are session-only, module scope survives
// TopNav remounts within the tab but resets on reload.
const dismissedEvents = new Set<string>()

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
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const avatarRef = useRef<HTMLDivElement>(null)
  const clock = useClock()
  const unreadCount = useGameStore((s) => s.unreadCount)
  const setNotifications = useGameStore((s) => s.setNotifications)
  const [notifOpen, setNotifOpen] = useState(false)
  const notifRef = useRef<HTMLDivElement>(null)
  const digestUnread = useGameStore((s) => s.digestUnread)
  const setDigestUnread = useGameStore((s) => s.setDigestUnread)
  const [digestOpen, setDigestOpen] = useState(false)
  const [commissionOpen, setCommissionOpen] = useState(false)
  const [shopOpen, setShopOpen] = useState(false)

  // Login streak + daily quest popup (D3)
  const [streakOpen, setStreakOpen] = useState(false)
  const [dailyData, setDailyData] = useState<DailyQuestResponse | null>(null)
  const streakRef = useRef<HTMLDivElement>(null)
  const streakBtnRef = useRef<HTMLButtonElement>(null)
  const [streakPopupPos, setStreakPopupPos] = useState<{ top: number; right: number }>({ top: 54, right: 16 })

  // Active world events banner (A2)
  const [events, setEvents] = useState<ActiveEventData[]>([])
  const [eventIdx, setEventIdx] = useState(0)
  const [bannerVisible, setBannerVisible] = useState(true)

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
  useEffect(() => {
    getMe()
      .then((me) => updateBalance(me.soul_coin_balance))
      .catch(() => {})
  }, [updateBalance])

  // D3: fetch quest/streak on mount; A2: fetch active events (60s server cache).
  useEffect(() => {
    refreshDailyQuest()
    getActiveEvents()
      .then((r) => setEvents(r.events.filter((e) => e.type !== 'season' && !dismissedEvents.has(e.id))))
      .catch(() => {})
  }, [refreshDailyQuest])

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
  }, [refreshDailyQuest])

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

  // Close the streak popup on outside click (popup is a DOM child of streakRef).
  useEffect(() => {
    if (!streakOpen) return
    const handler = (e: MouseEvent) => {
      if (streakRef.current && !streakRef.current.contains(e.target as Node)) setStreakOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [streakOpen])

  const toggleStreak = () => {
    const rect = streakBtnRef.current?.getBoundingClientRect()
    if (rect) {
      setStreakPopupPos({ top: rect.bottom + 6, right: Math.max(8, window.innerWidth - rect.right) })
    }
    setStreakOpen((v) => !v)
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
    if (!notifOpen) return
    const handler = (e: MouseEvent) => {
      if (notifRef.current && !notifRef.current.contains(e.target as Node)) setNotifOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [notifOpen])

  useEffect(() => {
    if (!dropdownOpen) return
    const handleClickOutside = (e: MouseEvent) => {
      if (avatarRef.current && !avatarRef.current.contains(e.target as Node)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [dropdownOpen])

  const handleLogout = () => {
    disconnectWS()
    logout()
    navigate('/login')
  }

  return (<>
    <nav style={{
      position: 'fixed', top: 0, left: 0, right: 0, height: 'var(--nav-height)',
      background: 'var(--bg-card)', borderBottom: '1px solid var(--border)',
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '0 16px', zIndex: 20,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <span style={{ fontWeight: 700, fontSize: 15, cursor: 'pointer' }}
              onClick={() => navigate('/play')}>🏙️ Simverse World</span>
        <button onClick={() => navigate('/forge')} style={{
          background: 'var(--accent-red)', color: 'white', border: 'none',
          padding: '5px 12px', borderRadius: 'var(--radius)', fontSize: 12,
          fontWeight: 600, cursor: 'pointer',
        }}>+ 炼化新居民</button>
        <button onClick={() => bridge.emit('bulletin:open')} style={{
          background: 'none', color: '#f59e0b', border: '1px solid #f59e0b44',
          padding: '5px 12px', borderRadius: 'var(--radius)', fontSize: 12,
          fontWeight: 600, cursor: 'pointer',
        }}>📋 公告板</button>
        <button onClick={() => navigate('/seasons')} style={{
          background: 'none', color: '#eab308', border: '1px solid #eab30844',
          padding: '5px 12px', borderRadius: 'var(--radius)', fontSize: 12,
          fontWeight: 600, cursor: 'pointer',
        }}>🏆 赛季</button>
        <button onClick={() => navigate('/debates')} style={{
          background: 'none', color: '#a78bfa', border: '1px solid #a78bfa44',
          padding: '5px 12px', borderRadius: 'var(--radius)', fontSize: 12,
          fontWeight: 600, cursor: 'pointer',
        }}>⚔️ 辩论</button>
        <button onClick={() => setShopOpen(true)} style={{
          background: 'none', color: '#f472b6', border: '1px solid #f472b644',
          padding: '5px 12px', borderRadius: 'var(--radius)', fontSize: 12,
          fontWeight: 600, cursor: 'pointer',
        }}>🛒 商店</button>
        <button onClick={() => setCommissionOpen(true)} style={{
          background: 'none', color: '#10b981', border: '1px solid #10b98144',
          padding: '5px 12px', borderRadius: 'var(--radius)', fontSize: 12,
          fontWeight: 600, cursor: 'pointer',
        }}>🗒️ 委托板</button>
        {user?.is_admin && (
          <button onClick={() => navigate('/admin')} style={{
            background: 'none', color: '#ef4444', border: '1px solid #ef444444',
            padding: '5px 12px', borderRadius: 'var(--radius)', fontSize: 12,
            fontWeight: 600, cursor: 'pointer',
          }}>🔐 管理</button>
        )}
      </div>
      <SearchDropdown />
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <span style={{
          color: 'var(--text-muted)', fontSize: 12,
          background: 'var(--bg-input)', padding: '4px 10px', borderRadius: 16,
          fontVariantNumeric: 'tabular-nums',
        }}>🕐 {clock}</span>
        <span style={{
          color: 'var(--accent-green)', fontSize: 13,
          background: '#53d76915', padding: '4px 12px', borderRadius: 16,
        }}>🪙 {balance}</span>
        {/* Login streak + daily quest (D3) */}
        <div ref={streakRef} style={{ position: 'relative' }}>
          <button
            ref={streakBtnRef}
            onClick={toggleStreak}
            title="连续登录"
            style={{
              background: 'var(--bg-input)', border: 'none', height: 30,
              padding: '0 10px', borderRadius: 15, cursor: 'pointer',
              fontSize: 12, fontWeight: 700, color: 'var(--text-primary)',
              display: 'flex', alignItems: 'center', gap: 2,
            }}
          >
            🔥{loginStreak}
          </button>
          {streakOpen && (
            <div style={{
              position: 'fixed', top: streakPopupPos.top, right: streakPopupPos.right,
              zIndex: 30, width: 264,
              background: 'var(--bg-card)', border: '1px solid var(--border)',
              borderRadius: 10, padding: 14, boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
            }}>
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
          onClick={() => { setDigestOpen(true); setDigestUnread(false) }}
          title="村落日报"
          style={{
            position: 'relative', background: 'var(--bg-input)', border: 'none',
            width: 30, height: 30, borderRadius: '50%', cursor: 'pointer',
            fontSize: 14, display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          📰
          {digestUnread && (
            <span style={{
              position: 'absolute', top: -2, right: -2, width: 9, height: 9,
              borderRadius: '50%', background: 'var(--accent-red)',
            }} />
          )}
        </button>
        <div ref={notifRef} style={{ position: 'relative' }}>
          <button
            onClick={() => setNotifOpen((v) => !v)}
            title="通知"
            style={{
              position: 'relative', background: 'var(--bg-input)', border: 'none',
              width: 30, height: 30, borderRadius: '50%', cursor: 'pointer',
              fontSize: 14, display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            🔔
            {unreadCount > 0 && (
              <span style={{
                position: 'absolute', top: -4, right: -4, minWidth: 16, height: 16,
                padding: '0 4px', borderRadius: 8, background: 'var(--accent-red)',
                color: 'white', fontSize: 10, fontWeight: 700,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>{unreadCount > 99 ? '99+' : unreadCount}</span>
            )}
          </button>
          {notifOpen && <NotificationDrawer onClose={() => setNotifOpen(false)} />}
        </div>
        <div ref={avatarRef} style={{ position: 'relative' }}>
          <div
            onClick={() => setDropdownOpen((v) => !v)}
            style={{
              width: 30, height: 30, background: 'var(--bg-input)', borderRadius: '50%',
              display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14,
              fontWeight: 700, color: 'var(--text-primary)', cursor: 'pointer',
              border: dropdownOpen ? '1px solid var(--accent)' : '1px solid transparent',
            }}
            title="账号菜单"
          >
            {user?.name?.[0]?.toUpperCase() || '?'}
          </div>
          {dropdownOpen && (
            <div style={{
              position: 'absolute', top: 38, right: 0,
              background: 'var(--bg-card)', border: '1px solid var(--border)',
              borderRadius: 8, minWidth: 160, boxShadow: '0 4px 16px rgba(0,0,0,0.3)',
              zIndex: 100, overflow: 'hidden',
            }}>
              <div style={{
                padding: '10px 14px', fontSize: 13, fontWeight: 600,
                color: 'var(--text-primary)', borderBottom: '1px solid var(--border)',
              }}>
                {user?.name ?? '用户'}
              </div>
              <button
                onClick={() => { setDropdownOpen(false); navigate('/profile') }}
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
                onClick={() => { setDropdownOpen(false); navigate('/capsules') }}
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
      {digestOpen && <DigestModal onClose={() => setDigestOpen(false)} />}
      {commissionOpen && <CommissionModal onClose={() => setCommissionOpen(false)} />}
      {shopOpen && <ShopModal onClose={() => setShopOpen(false)} />}
      {/* Mounted here (not GamePage) so the 公告板 button works on every
          authenticated page — the modal is self-contained (bridge + API). */}
      <BulletinBoard />
    </nav>
    {/* Active world-event banner (A2) — slim strip right below the nav.
        Overlays the map's top edge on purpose; the page does not reflow. */}
    {currentEvent && (
      <div style={{
        position: 'fixed', top: 'var(--nav-height)', left: 0, right: 0, zIndex: 19,
        background: 'rgba(233,69,96,0.12)', borderBottom: '1px solid rgba(233,69,96,0.35)',
        display: 'flex', alignItems: 'center', gap: 8, padding: '4px 16px',
        fontSize: 12, color: 'var(--text-primary)',
        opacity: bannerVisible ? 1 : 0, transition: 'opacity 0.3s ease',
      }}>
        <span>📣</span>
        <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          <span style={{ fontWeight: 600 }}>{currentEvent.title}</span>
          <span style={{ color: 'var(--text-secondary)' }}> — {currentEvent.description.slice(0, 80)}</span>
        </span>
        {events.length > 1 && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontVariantNumeric: 'tabular-nums' }}>
            {(eventIdx % events.length) + 1}/{events.length}
          </span>
        )}
        <button
          onClick={() => dismissEvent(currentEvent.id)}
          title="关闭"
          style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            fontSize: 12, cursor: 'pointer', padding: '2px 4px', lineHeight: 1,
          }}
          onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.color = 'var(--accent-red)' }}
          onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.color = 'var(--text-muted)' }}
        >✕</button>
      </div>
    )}
  </>)
}
