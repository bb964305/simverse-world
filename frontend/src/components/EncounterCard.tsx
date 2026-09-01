import { useEffect } from 'react'
import { useGameStore } from '../stores/gameStore'
import { sendWS } from '../services/ws'
import { useLocale } from '../services/locale'

/** B2: bottom-right encounter card; 10s auto-dismiss. Mounted once in App. */
export function EncounterCard() {
  const en = useLocale((state) => state.locale === 'en')
  const encounter = useGameStore((s) => s.pendingEncounter)
  const clear = useGameStore((s) => s.clearPendingEncounter)
  const openChat = useGameStore((s) => s.openChat)
  const chatOpen = useGameStore((s) => s.chatOpen)

  useEffect(() => {
    if (!encounter) return
    const id = setTimeout(clear, 10000)
    return () => clearTimeout(id)
  }, [encounter, clear])

  if (!encounter) return null

  const accept = () => {
    // Reuse the normal chat flow, carrying the scene context into the prompt.
    openChat({ slug: encounter.resident_slug, name: encounter.resident_name, role: '' })
    sendWS({ type: 'start_chat', resident_slug: encounter.resident_slug, context: encounter.opener })
    clear()
  }

  return (
    <div className={`game-encounter-card${chatOpen ? ' is-chat-open' : ''}`} style={{
      background: 'var(--bg-card)', border: '1px solid #0ea5e955',
      borderRadius: 8, padding: '14px 16px', boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
      animation: 'encSlideIn 0.35s ease',
    }}>
      <div style={{ fontSize: 12, color: '#0ea5e9', fontWeight: 700, marginBottom: 6 }}>✨ {en ? 'Encounter' : '偶遇'}</div>
      <div style={{ fontSize: 13, color: 'var(--text-primary)', lineHeight: 1.6, marginBottom: 12 }}>
        {encounter.opener}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <button onClick={accept} style={{
          flex: 1, background: 'var(--accent-blue)', color: 'white', border: 'none',
          padding: '8px', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer',
        }}>{en ? 'Say hello' : '打个招呼'}</button>
        <button onClick={clear} style={{
          background: 'var(--bg-input)', color: 'var(--text-muted)', border: 'none',
          padding: '8px 14px', borderRadius: 8, fontSize: 13, cursor: 'pointer',
        }}>{en ? 'Walk away' : '走开'}</button>
      </div>
      <style>{`@keyframes encSlideIn { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: translateY(0); } }`}</style>
    </div>
  )
}
