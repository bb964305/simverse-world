import { useEffect, useState } from 'react'
import { getWeeklyRecap, type WeeklyRecapData } from '../../services/api'

function StatChip({ label, value, suffix }: { label: string; value: number; suffix?: string }) {
  return (
    <div style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)',
      borderRadius: 10, padding: '12px 16px',
      display: 'flex', flexDirection: 'column', gap: 4, flex: '1 1 100px', minWidth: 100,
    }}>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 500 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text-primary)' }}>
        {value.toLocaleString()}
        {suffix && <span style={{ fontSize: 12, fontWeight: 400, color: 'var(--text-muted)', marginLeft: 3 }}>{suffix}</span>}
      </div>
    </div>
  )
}

/** Minimal md rendering: **bold** → <strong>; leading #s stripped (title shown separately). */
function renderInline(text: string) {
  const parts = text.split(/\*\*(.+?)\*\*/g)
  return parts.map((p, i) => (i % 2 === 1 ? <strong key={i} style={{ color: 'var(--text-primary)' }}>{p}</strong> : p))
}

export function WeeklyRecap() {
  const [recap, setRecap] = useState<WeeklyRecapData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Bumped by the retry button; the effect refetches whenever it changes.
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    getWeeklyRecap()
      .then((r) => { if (!cancelled) setRecap(r.digest) })
      .catch(() => { if (!cancelled) setError('回顾生成失败，请稍后重试') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [attempt])

  const retry = () => {
    setLoading(true)
    setError(null)
    setAttempt((a) => a + 1)
  }

  const paragraphs = (recap?.content_md ?? '')
    .split(/\n\n+/)
    .map((p) => p.replace(/^#+\s*/, '').trim())
    .filter((p) => p && p !== recap?.title)

  return (
    <div style={{ maxWidth: 640 }}>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }}>本周回顾</h2>
      <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 20 }}>
        这一周你在小镇留下的足迹
      </div>

      {loading ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>生成中…（首次生成可能需要一点时间）</div>
      ) : error ? (
        <div style={{ fontSize: 13 }}>
          <span style={{ color: 'var(--accent-red)' }}>{error}</span>
          <button onClick={retry} style={{
            marginLeft: 10, background: 'none', border: 'none',
            color: 'var(--accent-blue)', fontSize: 13, cursor: 'pointer',
          }}>重试</button>
        </div>
      ) : recap && (
        <>
          <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 14 }}>{recap.title}</div>

          {/* Stats chips */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: 16 }}>
            <StatChip label="对话" value={recap.stats.chats} suffix="次" />
            <StatChip label="轮次" value={recap.stats.turns} suffix="轮" />
            <StatChip label="居民" value={recap.stats.distinct_residents} suffix="位" />
            <StatChip label="成就" value={recap.stats.achievements} />
            <StatChip label="探索" value={recap.stats.explored} />
          </div>

          {/* Personality tag badge */}
          {recap.stats.tag && (
            <div style={{ marginBottom: 16 }}>
              <span style={{
                display: 'inline-block', padding: '4px 12px', borderRadius: 999,
                background: '#e9456018', border: '1px solid var(--accent-red)',
                color: 'var(--accent-red)', fontSize: 12, fontWeight: 700,
              }}>
                {recap.stats.tag}
              </span>
            </div>
          )}

          {/* Recap body (plain paragraphs, **bold** honored) */}
          <div style={{
            background: 'var(--bg-card)', border: '1px solid var(--border)',
            borderRadius: 10, padding: '18px 20px',
          }}>
            {paragraphs.map((p, i) => (
              <p key={i} style={{
                fontSize: 14, lineHeight: 1.75, color: 'var(--text-secondary)',
                margin: i === 0 ? 0 : '12px 0 0',
              }}>
                {renderInline(p)}
              </p>
            ))}
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 14 }}>{recap.date}</div>
          </div>
        </>
      )}
    </div>
  )
}
