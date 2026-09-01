import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useGameStore } from '../stores/gameStore'
import { SearchDropdown } from './SearchDropdown'
import { NotificationDrawer } from './NotificationDrawer'
import { DigestModal } from './DigestModal'
import { CommissionModal } from './CommissionModal'
import { BulletinBoard } from './BulletinBoard'
import { ExperimentPanel } from './ExperimentPanel'
import { TownHallPanel } from './TownHallPanel'
import { LabTerminalPanel } from './LabTerminalPanel'
import { MarketHallPanel } from './MarketHallPanel'
import { ShopModal } from './ShopModal'
import { bridge } from '../game/phaserBridge'
import { disconnectWS, onWSMessage } from '../services/ws'
import { getNotifications, getDailyQuest, getActiveEvents, getMe } from '../services/api'
import type { DailyQuestResponse, ActiveEventData } from '../services/api'
import {
  caravanBannerText, refreshCaravanProjection, subscribeCaravanProjection,
} from '../services/caravanProjection'
import type { CaravanState } from '../services/api'
import '../styles/game-ui.css'
import { disconnectWallet } from '../services/web3/wallet'
import { useLocale, type Locale } from '../services/locale'
import { BrandLogo } from './BrandLogo'
import { BrandSocialLinks } from './BrandSocialLinks'
import { LanguageToggle } from './LanguageToggle'
import { SIM_TOKEN } from '../config/simToken'
import { localizeWorldEvent } from '../services/worldLocalization'

const NAV_COPY = {
  en: {
    nav: 'Game navigation', home: 'Simverse World home', forge: '＋ Forge resident', agent: '◇ Onchain Agent',
    board: '📋 Bulletin', seasons: '🏆 Seasons', debates: '⚔️ Debates', shop: '🛒 Shop', commissions: '🗒️ Commissions',
    hall: '🏛️ Town Hall', market: '🏬 Market', lab: '🧪 Lab', terminal: '📊 Lab terminal', observatory: '◫ Observatory',
    world: 'World', openWorld: 'Open world menu', closeWorld: 'Close world menu', digest: '📰 Town digest', guide: '◎ New player guide', economy: '◉ SIM economy', buy: 'Buy SIM',
    account: 'Account menu', profile: '👤 Profile', capsules: '💌 Capsules', logout: 'Exit wallet session', studio: '◇ Onchain Agent Studio', community: 'Official community', user: 'Wallet resident',
    scBalance: 'Offchain game credits (SC)', streak: 'Login streak', streakDialog: 'Login streak and daily topic', streakDays: (days: number) => `${days}-day login streak 🔥`, dailyTopic: 'Daily topic', complete: '✅ Complete', active: 'In progress', topicReward: (turns: number, reward: number) => `Chat for ${turns} turns to earn ${reward} SC`, noTopic: 'No topic today', notifications: 'Notifications', closeEvent: 'Dismiss world event', caravan: 'Indigo Canopy Caravan', viewManifest: 'View goods', viewMarket: 'Open caravan market',
  },
  'zh-CN': {
    nav: '游戏主导航', home: '返回 Simverse World', forge: '＋ 炼化居民', agent: '◇ 链上 Agent',
    board: '📋 公告板', seasons: '🏆 赛季', debates: '⚔️ 辩论', shop: '🛒 商店', commissions: '🗒️ 委托',
    hall: '🏛️ 市政厅', market: '🏬 集市', lab: '🧪 实验楼', terminal: '📊 实验楼终端', observatory: '◫ 小镇观测站',
    world: '世界', openWorld: '打开世界菜单', closeWorld: '关闭世界菜单', digest: '📰 村落日报', guide: '◎ 新手教程', economy: '◉ SIM 经济模型', buy: '购买 SIM',
    account: '账号菜单', profile: '👤 个人主页', capsules: '💌 时间胶囊', logout: '退出钱包登录', studio: '◇ 链上 Agent 工作台', community: '官方社区', user: '钱包居民',
    scBalance: '链下游戏积分（SC）', streak: '连续登录', streakDialog: '连续登录与今日话题', streakDays: (days: number) => `连续登录 ${days} 天 🔥`, dailyTopic: '今日话题', complete: '✅ 已完成', active: '进行中', topicReward: (turns: number, reward: number) => `与 TA 聊满 ${turns} 轮可得 ${reward} SC`, noTopic: '今日暂无话题', notifications: '通知', closeEvent: '关闭世界事件', caravan: '靛篷商队', viewManifest: '查看货单', viewMarket: '查看商队集市',
  },
} as const

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

function useClock(locale: Locale) {
  const clockLocale = locale === 'en' ? 'en-US' : 'zh-CN'
  const [time, setTime] = useState(() => new Date().toLocaleTimeString(clockLocale, { hour: '2-digit', minute: '2-digit' }))
  useEffect(() => {
    const id = setInterval(() => {
      setTime(new Date().toLocaleTimeString(clockLocale, { hour: '2-digit', minute: '2-digit' }))
    }, 30_000)
    return () => clearInterval(id)
  }, [clockLocale])
  return time
}

export function TopNav() {
  const locale = useLocale((state) => state.locale)
  const copy = NAV_COPY[locale]
  const user = useGameStore((s) => s.user)
  const logout = useGameStore((s) => s.logout)
  const balance = user?.soul_coin_balance ?? 0
  const navigate = useNavigate()
  const [activePopover, setActivePopover] = useState<NavPopover>(null)
  const [activeModal, setActiveModal] = useState<NavModal>(null)
  const avatarRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const clock = useClock(locale)
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
  const [caravanState, setCaravanState] = useState<CaravanState | null>(null)
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
  // The experiment building is now a permanent visitor destination. This flag
  // only controls the operator terminal; ExperimentPanel reads the richer
  // /lab/status projection and renders a closed-beta/paused state itself.
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

  // The caravan is a server-authoritative temporary world entity. Subscribe to
  // the shared REST/WS convergence store so duplicate and out-of-order frames
  // cannot flicker the banner back to an older phase.
  useEffect(() => {
    const unsubscribe = subscribeCaravanProjection((projection) => {
      setCaravanState(projection.snapshot)
    })
    void refreshCaravanProjection().catch(() => { /* optional status copy */ })
    return unsubscribe
  }, [])

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
  const caravanStatus = caravanBannerText(caravanState, locale)
  useEffect(() => {
    // The caravan is a single live status, so do not blink it while background
    // world events continue to accumulate/cycle.
    if (caravanStatus || events.length <= 1) return
    const id = setInterval(() => {
      setBannerVisible(false)
      setTimeout(() => {
        setEventIdx((i) => i + 1)
        setBannerVisible(true)
      }, 300)
    }, 8000)
    return () => clearInterval(id)
  }, [caravanStatus, events.length])

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
  const localizedEvent = currentEvent ? localizeWorldEvent(currentEvent, locale) : null
  const hasTopBanner = Boolean(caravanStatus || currentEvent)

  useEffect(() => {
    document.documentElement.style.setProperty('--game-event-height', hasTopBanner ? '30px' : '0px')
    return () => document.documentElement.style.setProperty('--game-event-height', '0px')
  }, [hasTopBanner])

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
    const unsubMarket = bridge.on('market:open', closeLocalLayers)
    const unsubTownhall = bridge.on('townhall:open', closeLocalLayers)
    const unsubLabTerminal = bridge.on('labterminal:open', closeLocalLayers)
    return () => { unsubBulletin(); unsubExperiment(); unsubMarket(); unsubTownhall(); unsubLabTerminal() }
  }, [])

  const BRIDGE_PANELS = ['bulletin', 'experiment', 'market', 'townhall', 'labterminal'] as const
  type BridgePanel = (typeof BRIDGE_PANELS)[number]

  const closeBridgePanels = () => {
    for (const p of BRIDGE_PANELS) bridge.emit(`${p}:close`)
  }

  const openModal = (modal: Exclude<NavModal, null>) => {
    closeBridgePanels()
    setActivePopover(null)
    setActiveModal(modal)
  }

  const openBridgePanel = (panel: BridgePanel) => {
    setActiveModal(null)
    setActivePopover(null)
    // Close every other bridge panel so only one overlay owns the lane.
    for (const p of BRIDGE_PANELS) if (p !== panel) bridge.emit(`${p}:close`)
    bridge.emit(`${panel}:open`)
  }

  const navigateTo = (path: string) => {
    setActivePopover(null)
    navigate(path)
  }

  const handleLogout = () => {
    disconnectWS()
    void disconnectWallet()
    logout()
    navigate('/login')
  }

  return (<>
    <nav className="game-topnav" aria-label={copy.nav}>
      <div className="game-topnav__left">
        <button className="game-topnav__brand" onClick={() => navigateTo('/')} aria-label={copy.home}>
          <BrandLogo className="game-topnav__brand-mark" size={30} eager />
          <span className="game-topnav__brand-word">Simverse World</span>
        </button>
        <div className="game-topnav__links">
          <button onClick={() => navigateTo('/forge')} className="game-nav-link game-nav-link--primary">{copy.forge}</button>
          <button onClick={() => navigateTo('/web3')} className="game-nav-link game-nav-link--teal">{copy.agent}</button>
          <button onClick={() => openBridgePanel('bulletin')} className="game-nav-link game-nav-link--gold">{copy.board}</button>
          <button onClick={() => navigateTo('/seasons')} className="game-nav-link game-nav-link--gold">{copy.seasons}</button>
          <button onClick={() => navigateTo('/debates')} className="game-nav-link game-nav-link--violet">{copy.debates}</button>
          <button onClick={() => openModal('shop')} className="game-nav-link game-nav-link--pink">{copy.shop}</button>
          <button onClick={() => openModal('commission')} className="game-nav-link game-nav-link--green">{copy.commissions}</button>
          <button onClick={() => openBridgePanel('townhall')} className="game-nav-link game-nav-link--violet">{copy.hall}</button>
          {caravanState?.visible && <button onClick={() => openBridgePanel('market')} className="game-nav-link game-nav-link--gold">{copy.market}</button>}
          <button onClick={() => openBridgePanel('experiment')} className="game-nav-link game-nav-link--teal">{copy.lab}</button>
          {labEnabled && (
              <button onClick={() => openBridgePanel('labterminal')} className="game-nav-link game-nav-link--teal">{copy.terminal}</button>
          )}
          {user?.is_admin && (
            <button onClick={() => navigateTo('/admin')} className="game-nav-link game-nav-link--violet">{copy.observatory}</button>
          )}
        </div>
        <div ref={menuRef} className="game-topnav__control">
          <button
            className="game-topnav__menu-button"
            onClick={() => setActivePopover((current) => current === 'menu' ? null : 'menu')}
            aria-label={menuOpen ? copy.closeWorld : copy.openWorld}
            aria-expanded={menuOpen}
            aria-controls="game-world-menu"
          >
            <span aria-hidden="true">☰</span><span>{copy.world}</span>
          </button>
          {menuOpen && (
            <div id="game-world-menu" className="game-nav-menu" role="menu">
              <div className="game-nav-menu__search"><SearchDropdown /></div>
              <button onClick={() => navigateTo('/forge')} className="game-nav-link game-nav-link--primary" role="menuitem">{copy.forge}</button>
              <button onClick={() => navigateTo('/web3')} className="game-nav-link game-nav-link--teal" role="menuitem">{copy.agent}</button>
              <button onClick={() => navigateTo('/guide')} className="game-nav-link game-nav-link--teal" role="menuitem">{copy.guide}</button>
              <button onClick={() => navigateTo('/economy')} className="game-nav-link game-nav-link--gold" role="menuitem">{copy.economy}</button>
              <a href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer" className="game-nav-link game-nav-link--buy" role="menuitem" onClick={() => setActivePopover(null)}>{copy.buy} ↗</a>
              <button onClick={() => openBridgePanel('bulletin')} className="game-nav-link game-nav-link--gold" role="menuitem">{copy.board}</button>
              <button onClick={() => navigateTo('/seasons')} className="game-nav-link game-nav-link--gold" role="menuitem">{copy.seasons}</button>
              <button onClick={() => navigateTo('/debates')} className="game-nav-link game-nav-link--violet" role="menuitem">{copy.debates}</button>
              <button onClick={() => openModal('shop')} className="game-nav-link game-nav-link--pink" role="menuitem">{copy.shop}</button>
              <button onClick={() => openModal('commission')} className="game-nav-link game-nav-link--green" role="menuitem">{copy.commissions}</button>
              <button onClick={() => openBridgePanel('townhall')} className="game-nav-link game-nav-link--violet" role="menuitem">{copy.hall}</button>
              <button onClick={() => openBridgePanel('market')} className="game-nav-link game-nav-link--gold" role="menuitem">{copy.market}</button>
              <button onClick={() => openBridgePanel('experiment')} className="game-nav-link game-nav-link--teal" role="menuitem">{copy.lab}</button>
              {labEnabled && (
                <button onClick={() => openBridgePanel('labterminal')} className="game-nav-link game-nav-link--teal" role="menuitem">{copy.terminal}</button>
              )}
              <button onClick={() => { setDigestUnread(false); openModal('digest') }} className="game-nav-link" role="menuitem">{copy.digest}</button>
              {user?.is_admin && (
                <button onClick={() => navigateTo('/admin')} className="game-nav-link game-nav-link--violet" role="menuitem">{copy.observatory}</button>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="game-topnav__search"><SearchDropdown /></div>
      <div className="game-topnav__actions">
        <a className="game-topnav__buy" href={SIM_TOKEN.tradeUrl} target="_blank" rel="noopener noreferrer">{copy.buy} ↗</a>
        <span className="game-topnav__status game-topnav__clock" style={{ fontVariantNumeric: 'tabular-nums' }}>🕐 {clock}</span>
        <span className="game-topnav__status game-topnav__status--coin" title={copy.scBalance}>🪙 {balance} SC</span>
        {/* Login streak + daily quest (D3) */}
        <div ref={streakRef} className="game-topnav__control game-topnav__streak">
          <button
            ref={streakBtnRef}
            onClick={toggleStreak}
            title={copy.streak}
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
              aria-label={copy.streakDialog}
            >
              <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-primary)' }}>
                {copy.streakDays(loginStreak)}
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
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>{copy.dailyTopic}</div>
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
                        {quest.status === 'done' ? copy.complete : copy.active}
                      </span>
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4, lineHeight: 1.5 }}>
                      {quest.quest.topic}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
                      {copy.topicReward(quest.quest.min_turns, quest.reward_sc)}
                    </div>
                  </>
                ) : (
                  <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{copy.noTopic}</div>
                )}
              </div>
            </div>
          )}
        </div>
        <button
          onClick={() => { setDigestUnread(false); openModal('digest') }}
          title={copy.digest}
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
            title={copy.notifications}
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
            aria-label={copy.account}
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
                {user?.name ?? copy.user}
                {user?.wallet_address && (
                  <div style={{ marginTop: 4, color: 'var(--text-muted)', fontSize: 10, fontFamily: 'monospace' }}>
                    {user.wallet_address.slice(0, 8)}…{user.wallet_address.slice(-6)}
                  </div>
                )}
              </div>
              <div className="game-account-web3-tools">
                <LanguageToggle className="game-language-toggle" />
                <div><span>{copy.community}</span><BrandSocialLinks /></div>
              </div>
              <button
                onClick={() => navigateTo('/web3')}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  padding: '9px 14px', fontSize: 13,
                  color: '#2dd4bf', background: 'none', border: 'none',
                  cursor: 'pointer',
                }}
              >
                {copy.studio}
              </button>
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
                {copy.profile}
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
                {copy.capsules}
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
                🚪 {copy.logout}
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
    <MarketHallPanel />
    <TownHallPanel />
    <LabTerminalPanel />
    {/* The shared event-height variable moves every game HUD surface below it. */}
    {(caravanStatus || currentEvent) && (
      <div className="game-world-event" style={{ opacity: bannerVisible ? 1 : 0, transition: 'opacity 0.3s ease' }} role="status">
        <span>{caravanStatus ? '🛒' : '📣'}</span>
        <span className="game-world-event__copy">
          <span style={{ fontWeight: 600 }}>{caravanStatus ? copy.caravan : localizedEvent?.title}</span>
          <span className="game-world-event__description" style={{ color: '#d2c5c4' }}>
            {' · '}{caravanStatus ?? localizedEvent?.description.slice(0, 120)}
          </span>
        </span>
        {!caravanStatus && events.length > 1 && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontVariantNumeric: 'tabular-nums' }}>
            {(eventIdx % events.length) + 1}/{events.length}
          </span>
        )}
        {!caravanStatus && currentEvent && (
          <button
            onClick={() => dismissEvent(currentEvent.id)}
            aria-label={copy.closeEvent}
            className="game-dialog-close"
          >✕</button>
        )}
        {caravanStatus && (
          <button
            onClick={() => openBridgePanel('market')}
            className="game-dialog-close"
            aria-label={copy.viewMarket}
            style={{ width: 'auto', padding: '0 8px', fontSize: 11 }}
          >{copy.viewManifest}</button>
        )}
      </div>
    )}
  </>)
}
