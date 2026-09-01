import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getCreatorStats, type CreatorResidentStats, type CreatorStatsData } from '../../services/api'
import { useLocale } from '../../services/locale'
import { barRects, fillDailySeries, lineDots, linePath } from '../../utils/sparkline'

// ─── Static optimization-hint rules (D4: no LLM, plain heuristics) ──────────
function suggestionsFor(r: CreatorResidentStats, en: boolean): string[] {
  const out: string[] = []
  if (r.avg_rating_30d !== null && r.avg_rating_30d < 3.5) {
    out.push(en ? 'Rating below 3.5: check the persona for conflicting traits or an inconsistent voice.' : '评分偏低（<3.5）：检查 persona 中是否有互相冲突的设定，语气是否偏离角色定位')
  }
  if (r.conversations_30d === 0) {
    out.push(en ? 'No conversations in 30 days: strengthen the profile and opening line, or post on the bulletin board.' : '近 30 天没有对话：完善角色简介与开场白，或在公告栏发帖提高曝光')
  } else if (r.memories_30d === 0) {
    out.push(en ? 'No memories yet: invite longer conversations; deeper interactions are more likely to be remembered.' : '还没被小镇记住：引导更长的多轮对话，互动越深越容易留下记忆')
  }
  if (r.avg_rating_30d !== null && r.avg_rating_30d >= 4.5 && r.conversations_30d >= 5) {
    out.push(en ? 'Excellent performance: keep the persona stable and expand its skills to attract returning players.' : '表现优秀：保持人设稳定，可以尝试扩充能力设定吸引回头玩家')
  }
  return out
}

function StatChip({ label, value, suffix }: { label: string; value: string | number; suffix?: string }) {
  return (
    <div style={{
      background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10,
      padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 4, flex: '1 1 100px', minWidth: 100,
    }}>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 500 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--text-primary)' }}>
        {value}
        {suffix && <span style={{ fontSize: 12, fontWeight: 400, color: 'var(--text-muted)', marginLeft: 3 }}>{suffix}</span>}
      </div>
    </div>
  )
}

const CHART_W = 560

function BarChart({ values, color, height = 64 }: { values: number[]; color: string; height?: number }) {
  const rects = barRects(values, { width: CHART_W, height, pad: 2 })
  return (
    <svg viewBox={`0 0 ${CHART_W} ${height}`} preserveAspectRatio="none" style={{ width: '100%', height, display: 'block' }}>
      {rects.map((r, i) => (
        <rect key={i} x={r.x} y={r.y} width={r.width} height={r.height} fill={color} rx={1} opacity={0.85} />
      ))}
    </svg>
  )
}

function LineChart({ values, color, height = 64, min, max, emptyLabel }: {
  values: Array<number | null>; color: string; height?: number; min?: number; max?: number; emptyLabel: string
}) {
  const box = { width: CHART_W, height, pad: 4 }
  const domain = { min, max }
  const path = linePath(values, box, domain)
  const dots = lineDots(values, box, domain)
  if (!path) {
    return <div style={{ fontSize: 12, color: 'var(--text-muted)', padding: '20px 0', textAlign: 'center' }}>{emptyLabel}</div>
  }
  return (
    <svg viewBox={`0 0 ${CHART_W} ${height}`} style={{ width: '100%', height, display: 'block' }}>
      <path d={path} fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" />
      {dots.map((p, i) => <circle key={i} cx={p.x} cy={p.y} r={3} fill={color} />)}
    </svg>
  )
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10, padding: '14px 16px', flex: '1 1 260px', minWidth: 260 }}>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 600, marginBottom: 10 }}>{title}</div>
      {children}
    </div>
  )
}

function ResidentStatCell({ label, value }: { label: string; value: string | number }) {
  return (
    <div style={{ minWidth: 72 }}>
      <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{label}</div>
      <div style={{ fontSize: 15, fontWeight: 700 }}>{value}</div>
    </div>
  )
}

function ResidentStatsCard({ r, since, days, en }: { r: CreatorResidentStats; since: string; days: number; en: boolean }) {
  const daily = fillDailySeries(r.daily_conversations, since, days)
  const hints = suggestionsFor(r, en)
  return (
    <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10, padding: '14px 16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <div style={{ minWidth: 140 }}>
          <div style={{ fontWeight: 700, fontSize: 15 }}>{r.name}</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{'★'.repeat(Math.max(r.star_rating, 1))} · {r.slug}</div>
        </div>
        <ResidentStatCell label={en ? 'Chats (30 days)' : '对话（30天）'} value={r.conversations_30d} />
        <ResidentStatCell label={en ? 'Average rating' : '平均评分'} value={r.avg_rating_30d !== null ? r.avg_rating_30d.toFixed(1) : '—'} />
        <ResidentStatCell label={en ? 'SC earned' : 'SC 收益'} value={r.earnings_30d} />
        <ResidentStatCell label={en ? 'Memories' : '被记住'} value={r.memories_30d} />
        <div style={{ flex: '1 1 140px', minWidth: 120, alignSelf: 'stretch', display: 'flex', alignItems: 'center' }}>
          <svg viewBox="0 0 140 36" preserveAspectRatio="none" style={{ width: '100%', height: 36, display: 'block' }}>
            {barRects(daily, { width: 140, height: 36, pad: 1 }).map((b, i) => (
              <rect key={i} x={b.x} y={b.y} width={b.width} height={b.height} fill="var(--accent-blue, #4a9eff)" opacity={0.7} rx={0.5} />
            ))}
          </svg>
        </div>
      </div>
      {hints.length > 0 && (
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px dashed var(--border)' }}>
          {hints.map((h, i) => (
            <div key={i} style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.7 }}>💡 {h}</div>
          ))}
        </div>
      )}
    </div>
  )
}

function EmptyState({ en }: { en: boolean }) {
  const navigate = useNavigate()
  return (
    <div style={{ textAlign: 'center', padding: 48, color: 'var(--text-muted)' }}>
      <div style={{ fontSize: 48, marginBottom: 12 }}>📊</div>
      <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 6 }}>{en ? 'No creator data yet' : '还没有创作数据'}</div>
      <div style={{ fontSize: 13, marginBottom: 20 }}>{en ? 'Create your first resident to track conversations, ratings, SC credits, and memories here.' : '创建你的第一位居民，这里会展示 TA 的对话量、评分、收益与记忆足迹'}</div>
      <button onClick={() => navigate('/forge')} style={{
        background: 'var(--accent-red)', color: 'white', border: 'none', padding: '10px 24px',
        borderRadius: 'var(--radius)', fontSize: 14, fontWeight: 600, cursor: 'pointer',
      }}>
        {en ? 'Create a resident' : '去铸造所创建居民'}
      </button>
    </div>
  )
}

export function CreatorDashboard() {
  const en = useLocale((state) => state.locale === 'en')
  const [stats, setStats] = useState<CreatorStatsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    getCreatorStats()
      .then((d) => { if (!cancelled) setStats(d) })
      .catch(() => { if (!cancelled) setError(en ? 'Could not load creator statistics. Try again shortly.' : '统计加载失败，请稍后重试') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [attempt, en])

  const retry = () => { setLoading(true); setError(null); setAttempt((a) => a + 1) }

  if (loading) return <div style={{ color: 'var(--text-muted)', padding: 20, textAlign: 'center' }}>{en ? 'Loading…' : '加载中…'}</div>
  if (error || !stats) {
    return (
      <div style={{ fontSize: 13, padding: 20 }}>
        <span style={{ color: 'var(--accent-red)' }}>{error ?? (en ? 'Could not load statistics' : '统计加载失败')}</span>
        <button onClick={retry} style={{ marginLeft: 10, background: 'none', border: 'none', color: 'var(--accent-blue)', fontSize: 13, cursor: 'pointer' }}>{en ? 'Retry' : '重试'}</button>
      </div>
    )
  }

  return (
    <div style={{ maxWidth: 860 }}>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }}>{en ? 'Creator dashboard' : '创作者仪表盘'}</h2>
      <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 20 }}>
        {en ? `How your residents performed over the past ${stats.window_days} days` : `近 ${stats.window_days} 天你创作的居民在小镇里的表现`}
      </div>

      {stats.residents.length === 0 ? <EmptyState en={en} /> : (
        <>
          {/* Totals */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginBottom: 16 }}>
            <StatChip label={en ? 'Conversations' : '对话'} value={stats.totals.conversations} suffix={en ? undefined : '次'} />
            <StatChip label={en ? 'SC credits earned' : 'SC 收益'} value={stats.totals.earnings_sc} suffix="SC" />
            <StatChip label={en ? 'Memories' : '被记住'} value={stats.totals.memories} suffix={en ? undefined : '条'} />
            <StatChip label={en ? 'Average rating' : '平均评分'} value={stats.totals.avg_rating !== null ? stats.totals.avg_rating.toFixed(1) : '—'} />
          </div>

          {/* Trend charts */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, marginBottom: 20 }}>
            <ChartCard title={en ? 'Daily conversations' : '每日对话数'}>
              <BarChart values={stats.daily_conversations.map((p) => p.value)} color="var(--accent-blue, #4a9eff)" />
            </ChartCard>
            <ChartCard title={en ? 'Daily SC credits' : '每日 SC 收益'}>
              <BarChart values={stats.daily_earnings.map((p) => p.value)} color="var(--accent-green, #53d769)" />
            </ChartCard>
            <ChartCard title={en ? 'Weekly rating trend' : '评分趋势（按周）'}>
              <LineChart values={stats.weekly_ratings.map((w) => w.avg_rating)} color="var(--accent-red, #e94560)" min={1} max={5} emptyLabel={en ? 'No rating data yet' : '暂无评分数据'} />
            </ChartCard>
          </div>

          {/* Per-resident cards */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {stats.residents.map((r) => (
              <ResidentStatsCard key={r.id} r={r} since={stats.since} days={stats.window_days} en={en} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
