import { useEffect, useState } from 'react'
import { getAchievements, type AchievementEntry } from '../../services/api'

export function AchievementsPanel() {
  const [items, setItems] = useState<AchievementEntry[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    getAchievements()
      .then((r) => { if (!cancelled) setItems(r.achievements) })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const unlockedCount = items.filter((a) => a.unlocked).length

  return (
    <div>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }}>成就</h2>
      <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 20 }}>
        已解锁 {unlockedCount} / {items.length}
      </div>

      {loading ? (
        <div style={{ color: 'var(--text-muted)' }}>加载中…</div>
      ) : (
        <div style={{
          display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 14,
        }}>
          {items.map((a) => {
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
                <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-primary)' }}>{a.title}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4, minHeight: 30 }}>
                  {a.description}
                </div>
                {a.unlocked ? (
                  <div style={{ fontSize: 11, color: 'var(--accent-green)', marginTop: 6, fontWeight: 600 }}>
                    ✓ 已解锁 · +{a.reward_sc} 🪙
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
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>+{a.reward_sc} 🪙</div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
