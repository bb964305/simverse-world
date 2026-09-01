import { useEffect, useCallback } from 'react'
import { useGameStore } from '../stores/gameStore'
import { getNotifications, markNotificationsRead } from '../services/api'
import { useLocale } from '../services/locale'

const KIND_ICON: Record<string, string> = {
  resident_greeting: '👋',
  achievement: '🏆',
  capsule_delivered: '⏳',
  commission: '📜',
  feed: '📰',
  system: '🔔',
}

interface Props {
  onClose: () => void
}

export function NotificationDrawer({ onClose }: Props) {
  const locale = useLocale((state) => state.locale)
  const isEn = locale === 'en'
  const notifications = useGameStore((s) => s.notifications)
  const setNotifications = useGameStore((s) => s.setNotifications)

  useEffect(() => {
    let cancelled = false
    getNotifications()
      .then((r) => { if (!cancelled) setNotifications(r.notifications, r.unread_count) })
      .catch(() => { /* leave whatever WS pushed */ })
    return () => { cancelled = true }
  }, [setNotifications])

  const markAllRead = useCallback(async () => {
    const unreadIds = notifications.filter((n) => !n.read).map((n) => n.id)
    if (unreadIds.length === 0) return
    try {
      const r = await markNotificationsRead(unreadIds)
      setNotifications(
        notifications.map((n) => ({ ...n, read: true })),
        r.unread_count,
      )
    } catch { /* ignore */ }
  }, [notifications, setNotifications])

  return (
    <div role="region" aria-label={isEn ? 'Notifications' : '通知'} style={{
      position: 'absolute', top: 38, right: 0, width: 'min(320px, calc(100vw - 16px))', maxHeight: 'min(420px, calc(100dvh - 64px))',
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 8, boxShadow: '0 4px 16px rgba(0,0,0,0.3)', zIndex: 100,
      display: 'flex', flexDirection: 'column', overflow: 'hidden',
    }}>
      <div style={{
        padding: '10px 14px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <span style={{ fontWeight: 700, fontSize: 13 }}>{isEn ? 'Notifications' : '通知'}</span>
        <button onClick={() => void markAllRead()} style={{
          background: 'none', border: 'none', color: 'var(--accent-blue)',
          fontSize: 12, cursor: 'pointer',
        }}>{isEn ? 'Mark all read' : '全部已读'}</button>
      </div>
      <div style={{ overflowY: 'auto', flex: 1 }}>
        {notifications.length === 0 ? (
          <div style={{ padding: '24px 14px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
            {isEn ? 'No notifications' : '暂无通知'}
          </div>
        ) : notifications.map((n) => (
          <div key={n.id} style={{
            padding: '10px 14px', borderBottom: '1px solid var(--border)',
            background: n.read ? 'transparent' : '#0ea5e910',
            display: 'flex', gap: 10,
          }}>
            <span style={{ fontSize: 16, flexShrink: 0 }}>{KIND_ICON[n.kind] ?? '🔔'}</span>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>{n.title}</div>
              {n.body && (
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2, lineHeight: 1.5 }}>{n.body}</div>
              )}
            </div>
          </div>
        ))}
      </div>
      <button onClick={onClose} style={{
        padding: '8px', background: 'var(--bg-input)', border: 'none',
        borderTop: '1px solid var(--border)', color: 'var(--text-muted)',
        fontSize: 12, cursor: 'pointer',
      }}>{isEn ? 'Close' : '关闭'}</button>
    </div>
  )
}
