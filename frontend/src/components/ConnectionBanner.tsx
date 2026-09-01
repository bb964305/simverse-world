import { useGameStore } from '../stores/gameStore'
import { useLocale } from '../services/locale'

/**
 * Thin top-of-viewport strip shown while the WS connection is down and the
 * exponential-backoff reconnect loop is running (P2). Mounted once in App so
 * every page that opens the socket (GamePage, ForgePage, DebatesPage) is
 * covered; renders nothing when connected or after a deliberate disconnect.
 */
export function ConnectionBanner() {
  const locale = useLocale((state) => state.locale)
  const wsStatus = useGameStore((s) => s.wsStatus)
  const chatOpen = useGameStore((s) => s.chatOpen)
  if (wsStatus !== 'reconnecting') return null

  return (
    <div
      role="status"
      className={`game-connection-banner${chatOpen ? ' is-chat-open' : ''}`}
      style={{
        color: '#fffbeb',
        fontSize: 12, fontWeight: 600,
        textAlign: 'center', padding: '4px 12px',
        letterSpacing: 0,
      }}
    >
      {locale === 'en' ? 'Connection lost. Reconnecting…' : '连接已断开，正在重连…'}
    </div>
  )
}
