import { useEffect, useState } from 'react'
import { bridge } from '../game/phaserBridge'
import { STATUS_CONFIG } from '../game/StatusVisuals'
import { useGameStore } from '../stores/gameStore'
import { isFollowed, toggleFollow } from '../services/follows'
import type { ResidentData } from '../game/GameScene'

export function NpcTooltip() {
  const [npc, setNpc] = useState<ResidentData | null>(null)
  const [followed, setFollowedState] = useState(false)
  const [followBusy, setFollowBusy] = useState(false)
  const [followError, setFollowError] = useState<string | null>(null)
  const chatOpen = useGameStore((s) => s.chatOpen)

  useEffect(() => {
    return bridge.on('npc:nearby', (data: unknown) => setNpc(data as ResidentData | null))
  }, [])

  // Follow state lives client-side (services/follows.ts) — refresh per NPC.
  useEffect(() => {
    if (npc) {
      setFollowedState(isFollowed(npc.slug))
      setFollowError(null)
    }
  }, [npc])

  if (!npc || chatOpen) return null

  const handleFollow = async () => {
    if (followBusy) return
    setFollowBusy(true)
    setFollowError(null)
    try {
      setFollowedState(await toggleFollow(npc.slug))
    } catch (err) {
      const msg = err instanceof Error ? err.message : ''
      setFollowError(msg.includes('follow limit') ? '关注已达上限（50 位）' : '操作失败，请稍后重试')
    } finally {
      setFollowBusy(false)
    }
  }

  const cfg = STATUS_CONFIG[npc.status] ?? STATUS_CONFIG.idle

  return (
    <div style={{
      position: 'fixed', top: 60, right: 12, zIndex: 15, minWidth: 180,
      background: '#18181bf5', color: '#d4d4d8', fontSize: 13,
      padding: '10px 14px', borderRadius: 10, border: '1px solid var(--border)',
      backdropFilter: 'blur(8px)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 22 }}>🧑‍💻</span>
        <div>
          <div style={{ color: '#fafafa', fontWeight: 700, fontSize: 14 }}>{npc.name}</div>
          <div style={{ color: '#71717a', fontSize: 12 }}>
            {npc.meta_json?.role ?? ''}
            {npc.meta_json?.sbti && (
              <span style={{
                marginLeft: 4, fontSize: 9, padding: '0px 4px', borderRadius: 3,
                background: '#6c5ce7', color: '#fff', fontWeight: 600,
              }}>{npc.meta_json.sbti.type}</span>
            )}
          </div>
        </div>
        <span style={{ marginLeft: 'auto', fontSize: 11 }}>{cfg.label}</span>
      </div>
      {/* Follow toggle (E11) */}
      <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
        <button
          onClick={() => void handleFollow()}
          disabled={followBusy}
          style={{
            flex: 1, padding: '4px 10px', borderRadius: 6, fontSize: 11, fontWeight: 600,
            cursor: 'pointer', opacity: followBusy ? 0.5 : 1,
            background: followed ? 'var(--bg-input)' : '#e9456018',
            border: followed ? '1px solid var(--border)' : '1px solid var(--accent-red)',
            color: followed ? 'var(--text-muted)' : 'var(--accent-red)',
          }}
        >
          {followed ? '✓ 已关注' : '♥ 关注'}
        </button>
      </div>
      {followError && (
        <div style={{ color: 'var(--accent-red)', fontSize: 10, marginTop: 4, textAlign: 'center' }}>
          {followError}
        </div>
      )}
      <div style={{ color: '#52525b', fontSize: 11, marginTop: 6, textAlign: 'center' }}>
        {cfg.canChat
          ? <span>按 <kbd style={{ background: '#27272a', padding: '1px 5px', borderRadius: 3, color: '#fafafa', fontSize: 10 }}>E</kbd> 开始对话</span>
          : npc.status === 'sleeping' ? <span>💤 沉睡中 · 按 <kbd style={{ background: '#27272a', padding: '1px 5px', borderRadius: 3, color: '#fafafa', fontSize: 10 }}>E</kbd> 花费金币唤醒</span>
          : npc.status === 'chatting' ? <span>💬 对话中 · 按 <kbd style={{ background: '#27272a', padding: '1px 5px', borderRadius: 3, color: '#fafafa', fontSize: 10 }}>E</kbd> 排队等候</span>
          : '暂时无法对话'}
      </div>
    </div>
  )
}
