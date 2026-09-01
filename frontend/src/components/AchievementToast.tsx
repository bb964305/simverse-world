import { useEffect } from 'react'
import { useGameStore } from '../stores/gameStore'
import { useLocale } from '../services/locale'

/** Global celebratory toast for achievement unlocks (D1). Mounted once in App. */
export function AchievementToast() {
  const locale = useLocale((state) => state.locale)
  const toast = useGameStore((s) => s.achievementToast)
  const clear = useGameStore((s) => s.clearAchievementToast)
  const chatOpen = useGameStore((s) => s.chatOpen)

  useEffect(() => {
    if (!toast) return
    const id = setTimeout(clear, 5000)
    return () => clearTimeout(id)
  }, [toast, clear])

  if (!toast) return null

  return (
    <div
      onClick={clear}
      className={`game-toast-achievement${chatOpen ? ' is-chat-open' : ''}`}
      style={{
        cursor: 'pointer',
        background: '#f4c95d',
        color: '#1a1a1a', borderRadius: 8, padding: '12px 20px',
        boxShadow: '0 8px 32px rgba(245,158,11,0.4)',
        display: 'flex', alignItems: 'center', gap: 12,
        maxWidth: 'calc(100vw - 16px)',
        animation: 'achToastIn 0.4s ease',
      }}
    >
      <span style={{ fontSize: 28 }}>🏆</span>
      <div>
        <div style={{ fontSize: 11, fontWeight: 700, opacity: 0.8, letterSpacing: 0 }}>{locale === 'en' ? 'ACHIEVEMENT UNLOCKED' : '成就解锁'}</div>
        <div style={{ fontSize: 15, fontWeight: 800 }}>{toast.title}</div>
        {toast.reward_sc > 0 && (
          <div style={{ fontSize: 12, fontWeight: 600 }}>+{toast.reward_sc} 🪙</div>
        )}
      </div>
      <style>{`@keyframes achToastIn { from { opacity: 0; transform: translate(-50%, -12px); } to { opacity: 1; transform: translate(-50%, 0); } }`}</style>
    </div>
  )
}
