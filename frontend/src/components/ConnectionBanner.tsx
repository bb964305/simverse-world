import { useGameStore } from '../stores/gameStore'

/**
 * Thin top-of-viewport strip shown while the WS connection is down and the
 * exponential-backoff reconnect loop is running (P2). Mounted once in App so
 * every page that opens the socket (GamePage, ForgePage, DebatesPage) is
 * covered; renders nothing when connected or after a deliberate disconnect.
 */
export function ConnectionBanner() {
  const wsStatus = useGameStore((s) => s.wsStatus)
  if (wsStatus !== 'reconnecting') return null

  return (
    <div
      role="status"
      style={{
        position: 'fixed', top: 0, left: 0, right: 0,
        zIndex: 10000,
        background: 'linear-gradient(90deg, #b45309, #d97706)',
        color: '#fffbeb',
        fontSize: 12, fontWeight: 600,
        textAlign: 'center', padding: '4px 12px',
        letterSpacing: '0.3px',
        boxShadow: '0 2px 8px rgba(0,0,0,0.25)',
      }}
    >
      连接已断开，正在重连…
    </div>
  )
}
