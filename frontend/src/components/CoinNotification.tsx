import { useState, useEffect, useCallback, useRef } from 'react'
import { onWSMessage } from '../services/ws'
import { useGameStore } from '../stores/gameStore'
import { useLocale } from '../services/locale'

interface CoinNotif {
  id: number
  amount: number
  reason: string
}

let notifCounter = 0

const REASON_LABELS: Record<string, readonly [string, string]> = {
  daily_login_reward: ['Daily reward', '每日奖励'],
  creator_passive: ['Creator earnings', '创作者收益'],
  skill_creation: ['Skill creation', 'Skill 炼化'],
  chat: ['Conversation', '对话'],
  signup_bonus: ['Welcome bonus', '新手礼包'],
  good_rating: ['Rating reward', '好评奖励'],
}

export function CoinNotification() {
  const locale = useLocale((state) => state.locale)
  const [notifications, setNotifications] = useState<CoinNotif[]>([])
  const timersRef = useRef(new Map<number, ReturnType<typeof setTimeout>>())
  const chatOpen = useGameStore((s) => s.chatOpen)

  const add = useCallback((amount: number, reason: string) => {
    const id = ++notifCounter
    setNotifications((prev) => [...prev, { id, amount, reason }])
    const t = setTimeout(() => {
      setNotifications((prev) => prev.filter((n) => n.id !== id))
      timersRef.current.delete(id)
    }, 3000)
    timersRef.current.set(id, t)
  }, [])

  useEffect(() => {
    const unsub = onWSMessage((data) => {
      if (data.type === 'coin_earned' && typeof data.amount === 'number') {
        add(data.amount as number, (data.reason as string) || 'coin_earned')
      }
      if (data.type === 'daily_reward' && typeof data.amount === 'number') {
        add(data.amount as number, 'daily_login_reward')
      }
      if (data.type === 'coin_update' && typeof data.delta === 'number' && (data.delta as number) < 0) {
        add(data.delta as number, (data.reason as string) || 'chat')
      }
    })
    return unsub
  }, [add])

  useEffect(() => () => { timersRef.current.forEach(clearTimeout) }, [])

  if (notifications.length === 0) return null

  return (
    <div className={`game-toast-coins${chatOpen ? ' is-chat-open' : ''}`} style={{
      display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6,
      pointerEvents: 'none',
    }}>
      {notifications.map((n) => (
        <div key={n.id} style={{
          padding: '8px 18px', borderRadius: 20, fontSize: 14, fontWeight: 700,
          animation: 'coinFloatUp 3s ease-out forwards',
          background: n.amount > 0 ? 'rgba(16, 32, 26, 0.94)' : 'rgba(42, 19, 23, 0.94)',
          color: n.amount > 0 ? '#53d769' : '#e94560',
          border: `1px solid ${n.amount > 0 ? '#53d76940' : '#e9456040'}`,
          backdropFilter: 'blur(8px)',
          whiteSpace: 'nowrap', maxWidth: 'calc(100vw - 16px)', overflow: 'hidden', textOverflow: 'ellipsis',
        }}>
          🪙 {n.amount > 0 ? '+' : ''}{n.amount}
          <span style={{ color: 'var(--text-muted)', fontSize: 11, marginLeft: 8 }}>
            {REASON_LABELS[n.reason]?.[locale === 'en' ? 0 : 1] ?? n.reason}
          </span>
        </div>
      ))}
    </div>
  )
}
