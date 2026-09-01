import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { keccak256, toHex } from 'viem'
import { getAchievements, type AchievementEntry } from '../../services/api'
import { useLocale } from '../../services/locale'
import { loadOwnedAgents, recordWorldProof, registryConfigured } from '../../services/web3/agentRegistry'
import { useGameStore } from '../../stores/gameStore'

const ACHIEVEMENT_EN: Record<string, { title: string; description: string }> = {
  first_chat: { title: 'First Contact', description: 'Complete your first resident conversation.' },
  deep_talk: { title: 'Deep Conversation', description: 'Reach a meaningful multi-turn conversation.' },
  remembered: { title: 'Remembered', description: 'Leave your first lasting resident memory.' },
  memory_keeper_10: { title: 'Memory Keeper', description: 'Create 10 lasting resident memories.' },
  soul_shaper: { title: 'Soul Shaper', description: 'Trigger a resident personality evolution for the first time.' },
  week_streak: { title: 'Seven-Day Visitor', description: 'Visit Simverse for seven consecutive days.' },
  explorer_5: { title: 'Town Explorer', description: 'Discover five named locations.' },
  explorer_all: { title: 'World Cartographer', description: 'Discover every named location.' },
  errand_runner: { title: 'Reliable Hand', description: 'Complete a town commission.' },
  patron: { title: 'Local Patron', description: 'Support the town economy with SC gameplay credits.' },
  socialite: { title: 'Social Spark', description: 'Build connections across the town.' },
  dreamt_of: { title: 'Dream Visitor', description: 'Appear in a resident’s dream.' },
}

export function AchievementsPanel() {
  const locale = useLocale((state) => state.locale)
  const en = locale === 'en'
  const wallet = useGameStore((state) => state.user?.wallet_address) as `0x${string}` | undefined
  const [items, setItems] = useState<AchievementEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [anchoring, setAnchoring] = useState(false)
  const [anchorError, setAnchorError] = useState('')
  const [anchorTx, setAnchorTx] = useState<`0x${string}` | null>(null)

  useEffect(() => {
    let cancelled = false
    getAchievements()
      .then((r) => { if (!cancelled) setItems(r.achievements) })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const unlockedCount = items.filter((a) => a.unlocked).length

  const anchorUnlocked = async () => {
    if (!wallet || !registryConfigured()) return
    setAnchoring(true); setAnchorError(''); setAnchorTx(null)
    try {
      const agents = await loadOwnedAgents(wallet)
      if (!agents.length) throw new Error(en ? 'Create an Agent Passport before anchoring achievements.' : '请先创建 Agent Passport，再写入成就证明。')
      const unlocked = items.filter((item) => item.unlocked).map((item) => item.code).sort()
      const revision = BigInt(Date.now())
      const hash = keccak256(toHex(JSON.stringify({ schema: 'simverse-achievement-proof-v1', wallet: wallet.toLowerCase(), unlocked, revision: revision.toString() })))
      const tx = await recordWorldProof(locale, wallet, agents[0].id, keccak256(toHex('simverse.achievements.v1')), hash, revision)
      setAnchorTx(tx)
    } catch (reason) {
      setAnchorError(reason instanceof Error ? reason.message : (en ? 'Could not anchor achievements.' : '成就证明上链失败。'))
    } finally { setAnchoring(false) }
  }

  return (
    <div>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }}>{en ? 'Achievements' : '成就'}</h2>
      <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 20 }}>
        {en ? `Unlocked ${unlockedCount} / ${items.length}` : `已解锁 ${unlockedCount} / ${items.length}`}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 16 }}>
        <button type="button" onClick={() => void anchorUnlocked()} disabled={anchoring || unlockedCount === 0 || !wallet || !registryConfigured()} style={{
          border: '1px solid var(--accent-green)', background: '#53d76918', color: 'var(--accent-green)',
          borderRadius: 7, padding: '8px 12px', cursor: anchoring ? 'wait' : 'pointer', fontSize: 12, fontWeight: 700,
          opacity: anchoring || unlockedCount === 0 || !wallet || !registryConfigured() ? 0.55 : 1,
        }}>{anchoring ? (en ? 'Confirming onchain…' : '等待链上确认…') : (en ? 'Anchor unlocked achievements' : '将已解锁成就写入链上证明')}</button>
        <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>{en ? 'Uses your first Passport and requires one wallet confirmation.' : '使用第一个 Passport，需要钱包确认一笔交易。'}</span>
        {!wallet && <Link to="/login" style={{ color: 'var(--accent-blue)', fontSize: 11 }}>{en ? 'Connect wallet' : '连接钱包'}</Link>}
      </div>
      {anchorTx && <div role="status" style={{ color: 'var(--accent-green)', fontSize: 12, marginBottom: 14 }}>{en ? 'Proof confirmed: ' : '证明已确认：'}<a href={`https://robinhoodchain.blockscout.com/tx/${anchorTx}`} target="_blank" rel="noreferrer">{anchorTx.slice(0, 10)}… ↗</a></div>}
      {anchorError && <div role="alert" style={{ color: 'var(--accent-red)', fontSize: 12, marginBottom: 14 }}>{anchorError}</div>}

      {loading ? (
        <div style={{ color: 'var(--text-muted)' }}>{en ? 'Loading…' : '加载中…'}</div>
      ) : (
        <div style={{
          display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 14,
        }}>
          {items.map((a) => {
            const localized = en ? ACHIEVEMENT_EN[a.code] : null
            const pct = a.progress?.target
              ? Math.min(100, Math.round(((a.progress.count ?? 0) / a.progress.target) * 100))
              : 0
            return (
              <div key={a.code} style={{
                background: 'var(--bg-card)', border: '1px solid var(--border)',
                borderRadius: 10, padding: '14px', textAlign: 'center',
                opacity: a.unlocked ? 1 : 0.55,
                filter: a.unlocked ? 'none' : 'grayscale(0.8)',
              }}>
                <div style={{ fontSize: 34, marginBottom: 6 }}>{a.icon}</div>
                <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-primary)' }}>{localized?.title ?? a.title}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4, minHeight: 30 }}>
                  {localized?.description ?? a.description}
                </div>
                {a.unlocked ? (
                  <div style={{ fontSize: 11, color: 'var(--accent-green)', marginTop: 6, fontWeight: 600 }}>
                    ✓ {en ? 'Unlocked' : '已解锁'} · +{a.reward_sc} SC
                  </div>
                ) : a.progress?.target ? (
                  <div style={{ marginTop: 8 }}>
                    <div style={{ height: 5, background: 'var(--bg-input)', borderRadius: 3, overflow: 'hidden' }}>
                      <div style={{ width: `${pct}%`, height: '100%', background: 'var(--accent-blue)' }} />
                    </div>
                    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 3 }}>
                      {a.progress.count ?? 0} / {a.progress.target}
                    </div>
                  </div>
                ) : (
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>+{a.reward_sc} SC</div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
