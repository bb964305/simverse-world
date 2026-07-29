import { useEffect, useState } from 'react'
import { bridge } from '../game/phaserBridge'
import { STATUS_CONFIG } from '../game/StatusVisuals'
import { useGameStore } from '../stores/gameStore'
import { isFollowed, toggleFollow } from '../services/follows'
import { getResidentGoals, getMe, investInGoal } from '../services/api'
import type { ResidentGoalData } from '../services/api'
import type { ResidentData } from '../game/GameScene'

// E1 mood word → emoji (backend label set in mood_service.label_for).
const MOOD_EMOJI: Record<string, string> = {
  calm: '😌', content: '😊', excited: '🤩', annoyed: '😤',
  furious: '😠', gloomy: '😔', anxious: '😰', tired: '😴',
}

// Goal cache (A1): as the player walks around, 'npc:nearby' fires per NPC —
// cache the last N slugs for a short TTL so we don't refetch on every pass.
const goalCache = new Map<string, { goal: ResidentGoalData | null; at: number }>()
const GOAL_CACHE_TTL_MS = 60_000
const GOAL_CACHE_MAX = 20

// Goal investment (E13): backend enforces 50-500 SC per bet, 2000 SC pool cap.
const INVEST_AMOUNTS = [50, 100, 200, 500]

interface InvestNotice {
  kind: 'ok' | 'err'
  text: string
}

// apiFetch surfaces backend 400s as `API 400: {"detail":"..."}` — map the
// known invest failures to short, human-readable Chinese.
function investErrorText(err: unknown): string {
  const msg = err instanceof Error ? err.message : ''
  if (msg.includes('Insufficient')) return '灵魂币不足'
  if (msg.includes('pool cap')) return '该目标投资池已满'
  if (msg.includes('not an active life goal')) return '该目标已结束，无法投资'
  return '投资失败，请稍后重试'
}

export function NpcTooltip() {
  const [npc, setNpc] = useState<ResidentData | null>(null)
  const [followed, setFollowedState] = useState(false)
  const [followBusy, setFollowBusy] = useState(false)
  const [followError, setFollowError] = useState<string | null>(null)
  const [goal, setGoal] = useState<ResidentGoalData | null>(null)
  const [investBusy, setInvestBusy] = useState(false)
  const [investNotice, setInvestNotice] = useState<InvestNotice | null>(null)
  const chatOpen = useGameStore((s) => s.chatOpen)
  const updateBalance = useGameStore((s) => s.updateBalance)

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

  // Active life goal (A1) — fetch only when the slug actually changes; serve
  // from the TTL cache when fresh. Silent-fail: the tooltip must never break.
  const npcSlug = npc?.slug ?? null
  useEffect(() => {
    setInvestNotice(null)
    if (!npcSlug) {
      setGoal(null)
      return
    }
    const cached = goalCache.get(npcSlug)
    if (cached && Date.now() - cached.at < GOAL_CACHE_TTL_MS) {
      setGoal(cached.goal)
      return
    }
    setGoal(null)
    let cancelled = false
    getResidentGoals(npcSlug)
      .then((r) => {
        // Re-insert so Map iteration order doubles as LRU-ish eviction order.
        goalCache.delete(npcSlug)
        goalCache.set(npcSlug, { goal: r.active, at: Date.now() })
        while (goalCache.size > GOAL_CACHE_MAX) {
          const oldest = goalCache.keys().next().value
          if (oldest === undefined) break
          goalCache.delete(oldest)
        }
        if (!cancelled) setGoal(r.active)
      })
      .catch(() => { /* silent — tooltip stays functional without goal data */ })
    return () => { cancelled = true }
  }, [npcSlug])

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

  // Invest in the active life goal (E13): charge via API, then refresh the
  // balance through the store's existing mechanism (getMe → updateBalance).
  const handleInvest = async (amount: number) => {
    if (investBusy || !goal) return
    setInvestBusy(true)
    setInvestNotice(null)
    try {
      await investInGoal(goal.id, amount)
      setInvestNotice({ kind: 'ok', text: `已投资 ${amount} 🪙，目标达成享 1.5 倍分红` })
      getMe().then((me) => updateBalance(me.soul_coin_balance)).catch(() => {})
    } catch (err) {
      setInvestNotice({ kind: 'err', text: investErrorText(err) })
    } finally {
      setInvestBusy(false)
    }
  }

  const cfg = STATUS_CONFIG[npc.status] ?? STATUS_CONFIG.idle
  // Goal card derived values (A1)
  const goalPct = goal ? Math.round(Math.min(1, Math.max(0, goal.progress)) * 100) : 0
  const latestDoneMilestone = goal?.milestones?.filter((m) => m.done).pop() ?? null

  return (
    <div className="game-shell__npc-tooltip" style={{
      background: '#18181bf5', color: '#d4d4d8', fontSize: 13,
      padding: '10px 14px', borderRadius: 8, border: '1px solid var(--border)',
      backdropFilter: 'blur(8px)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 22 }}>🧑‍💻</span>
        <div>
          <div style={{ color: '#fafafa', fontWeight: 700, fontSize: 14 }}>{npc.name}</div>
          <div style={{ color: '#71717a', fontSize: 12 }}>
            {npc.meta_json?.role ?? ''}
            {npc.meta_json?.role && npc.meta_json?.sbti && ' · '}
            {npc.meta_json?.sbti && (
              <span style={{
                marginLeft: 4, fontSize: 9, padding: '0px 4px', borderRadius: 3,
                background: '#6c5ce7', color: '#fff', fontWeight: 600,
              }}>{npc.meta_json.sbti.type}</span>
            )}
          </div>
        </div>
        <span style={{ marginLeft: 'auto', fontSize: 11 }} title={npc.mood_label ?? 'calm'}>
          {MOOD_EMOJI[npc.mood_label ?? 'calm'] ?? '😌'} {cfg.label}
        </span>
      </div>
      {/* Active life goal (A1) — only when the resident has one */}
      {goal && (
        <div style={{
          marginTop: 8, padding: '6px 8px', background: 'var(--bg-input)',
          borderRadius: 6, border: '1px solid var(--border)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: '#d4d4d8' }}>
            <span>🎯</span>
            <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600 }}>
              {goal.title}
            </span>
          </div>
          <div style={{ marginTop: 5, height: 4, background: '#27272a', borderRadius: 2, overflow: 'hidden' }}>
            <div style={{
              height: '100%', width: `${goalPct}%`,
              background: 'var(--accent-green)', borderRadius: 2,
              transition: 'width 0.3s ease',
            }} />
          </div>
          <div style={{ marginTop: 3, fontSize: 10, color: 'var(--text-muted)' }}>进度 {goalPct}%</div>
          {latestDoneMilestone && (
            <div style={{
              marginTop: 2, fontSize: 10, color: 'var(--text-muted)',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>
              ✓ {latestDoneMilestone.title}
            </div>
          )}
          {/* Goal investment (E13): pick an amount → invest → refresh balance */}
          <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ fontSize: 10, color: 'var(--text-muted)', marginRight: 2 }}>💰 投资</span>
            {INVEST_AMOUNTS.map((amt) => (
              <button
                key={amt}
                onClick={() => void handleInvest(amt)}
                disabled={investBusy}
                title={`投资 ${amt} 灵魂币`}
                style={{
                  flex: 1, padding: '2px 0', borderRadius: 4, fontSize: 10, fontWeight: 600,
                  cursor: investBusy ? 'default' : 'pointer', opacity: investBusy ? 0.5 : 1,
                  background: 'var(--bg-input)', border: '1px solid var(--border)',
                  color: '#eab308',
                }}
              >
                {amt}
              </button>
            ))}
          </div>
          {investNotice && (
            <div style={{
              marginTop: 4, fontSize: 10, textAlign: 'center',
              color: investNotice.kind === 'ok' ? 'var(--accent-green)' : 'var(--accent-red)',
            }}>
              {investNotice.text}
            </div>
          )}
        </div>
      )}
      {/* Follow toggle (E11) + group photo (E10) */}
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
        <button
          onClick={() => bridge.emit('photo:take', { residentSlug: npc.slug })}
          title="合影"
          style={{
            padding: '4px 10px', borderRadius: 6, fontSize: 11, fontWeight: 600,
            cursor: 'pointer', background: 'var(--bg-input)',
            border: '1px solid var(--border)', color: 'var(--text-secondary)',
          }}
        >
          📸 合影
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
