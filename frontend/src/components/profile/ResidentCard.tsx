import { useEffect, useState } from 'react'

interface SbtiInfo {
  type: string
  type_name: string
}

interface ResidentCardProps {
  resident: {
    id: string
    slug: string
    name: string
    star_rating: number
    status: string
    heat: number
    district: string
    total_conversations: number
    avg_rating: number
    sprite_key: string
    meta_json: { role?: string; sbti?: SbtiInfo } | null
  }
  onEdit: (slug: string) => void
  /** Optional: pass the latest WS message so ResidentCard can react to type changes */
  lastWsMessage?: { type: string; resident_id: string; new_type: string; type_name: string } | null
}

const STATUS_LABELS: Record<string, string> = {
  idle: '🟢 空闲',
  chatting: '💬 对话中',
  sleeping: '💤 沉睡',
  popular: '🔥 热门',
}

export function ResidentCard({ resident, onEdit, lastWsMessage }: ResidentCardProps) {
  // flashSeq: 0 = idle; each accepted WS type-change bumps it so the timer
  // effect below re-arms (the old clearTimeout + setTimeout per message).
  const [flashSeq, setFlashSeq] = useState(0)
  const [displayedType, setDisplayedType] = useState<SbtiInfo | undefined>(
    resident.meta_json?.sbti ?? undefined
  )

  // React to resident_type_changed WS messages. Formerly an effect with deps
  // [lastWsMessage, resident.id]; now the same check as a render-time state
  // adjustment (react-hooks/set-state-in-effect). prevWs starts as null so
  // the first render processes the current message, matching the old
  // effect's initial run on mount.
  const [prevWs, setPrevWs] = useState<{ msg: ResidentCardProps['lastWsMessage']; id: string } | null>(null)
  if (prevWs === null || prevWs.msg !== lastWsMessage || prevWs.id !== resident.id) {
    setPrevWs({ msg: lastWsMessage, id: resident.id })
    if (
      lastWsMessage?.type === 'resident_type_changed' &&
      lastWsMessage.resident_id === resident.id
    ) {
      setDisplayedType({ type: lastWsMessage.new_type, type_name: lastWsMessage.type_name })
      setFlashSeq((n) => n + 1)
    }
  }

  // Sync displayed type with prop updates (e.g. after page reload). Formerly
  // an effect keyed on sbti.type; now the identical comparison during render.
  const sbti = resident.meta_json?.sbti
  const [prevSbtiType, setPrevSbtiType] = useState(sbti?.type)
  if (sbti?.type !== prevSbtiType) {
    setPrevSbtiType(sbti?.type)
    if (sbti) setDisplayedType(sbti)
  }

  // Flash timer: re-arms whenever flashSeq bumps (extending the flash on
  // rapid messages, as before); cleanup covers both the old "clear previous
  // timer on new message" and "clear on unmount" paths.
  useEffect(() => {
    if (flashSeq === 0) return
    const timer = setTimeout(() => setFlashSeq(0), 1500)
    return () => clearTimeout(timer)
  }, [flashSeq])

  const isFlashing = flashSeq > 0

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 14, padding: '14px 16px',
      background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 12,
    }}>
      <div style={{
        width: 48, height: 48, background: 'var(--bg-input)', borderRadius: 8,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 24, flexShrink: 0,
      }}>🧑‍💻</div>

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontWeight: 700, fontSize: 14 }}>{resident.name}</span>
          <span style={{ fontSize: 12 }}>{'⭐'.repeat(resident.star_rating)}</span>
          {displayedType && (
            <span
              style={{
                fontSize: 10,
                padding: '1px 6px',
                borderRadius: 4,
                background: isFlashing ? 'var(--accent-alt, #fd79a8)' : 'var(--accent, #6c5ce7)',
                color: '#fff',
                fontWeight: 600,
                letterSpacing: 0.5,
                transition: 'background 0.3s ease',
                transform: isFlashing ? 'scale(1.15)' : 'scale(1)',
                display: 'inline-block',
              }}
              title={displayedType.type_name}
            >
              {displayedType.type}
            </span>
          )}
        </div>

        <div style={{ color: 'var(--text-muted)', fontSize: 12, marginTop: 2 }}>
          {resident.meta_json?.role ?? ''} · {resident.district}
        </div>

        <div style={{ display: 'flex', gap: 12, marginTop: 4, fontSize: 11, color: 'var(--text-secondary)' }}>
          <span>{STATUS_LABELS[resident.status] ?? resident.status}</span>
          <span>🔥 {resident.heat}</span>
          <span>💬 {resident.total_conversations}</span>
          {resident.avg_rating > 0 && <span>⭐ {resident.avg_rating.toFixed(1)}</span>}
        </div>
      </div>

      <button
        onClick={() => onEdit(resident.slug)}
        style={{
          background: 'var(--bg-input)', border: '1px solid var(--border)',
          color: 'var(--text-secondary)', padding: '6px 14px', borderRadius: 6,
          fontSize: 12, cursor: 'pointer', flexShrink: 0,
        }}
      >
        编辑
      </button>
    </div>
  )
}
