import { useEffect, useState } from 'react'
import { TopNav } from '../components/TopNav'
import { useGameStore } from '../stores/gameStore'
import {
  getCurrentSeason,
  getSeasonLeaderboard,
  getOpenPolls,
  votePoll,
  type SeasonInfo,
  type LeaderboardResponse,
  type PollData,
} from '../services/api'

// Pull the backend `detail` string out of an apiFetch error ("API 400: {json}").
function errDetail(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e)
  const m = msg.match(/^API \d+: ([\s\S]*)$/)
  if (m) {
    try {
      const parsed = JSON.parse(m[1]) as { detail?: unknown }
      if (typeof parsed.detail === 'string') return parsed.detail
    } catch { /* raw body wasn't JSON — fall through */ }
  }
  return msg
}

function isNotFound(e: unknown): boolean {
  return e instanceof Error && e.message.startsWith('API 404')
}

function formatCountdown(endsAt: string, now: number): string {
  const diff = new Date(endsAt).getTime() - now
  if (Number.isNaN(diff)) return '—'
  if (diff <= 0) return '已结束'
  const d = Math.floor(diff / 86_400_000)
  const h = Math.floor((diff % 86_400_000) / 3_600_000)
  const m = Math.floor((diff % 3_600_000) / 60_000)
  const s = Math.floor((diff % 60_000) / 1000)
  if (d > 0) return `${d} 天 ${h} 小时 ${m} 分`
  if (h > 0) return `${h} 小时 ${m} 分 ${s} 秒`
  return `${m} 分 ${s} 秒`
}

const SEASON_STATUS_LABEL: Record<string, string> = {
  active: '进行中',
  settled: '已结算',
}

const cardStyle: React.CSSProperties = {
  background: 'var(--bg-card)',
  border: '1px solid var(--border)',
  borderRadius: 10,
  padding: 20,
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-primary)', margin: '28px 0 12px' }}>
      {children}
    </div>
  )
}

// ── Season header ───────────────────────────────────────────────────
function SeasonHeader({ season, loading, error }: { season: SeasonInfo | null; loading: boolean; error: string }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!season?.ends_at) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [season?.ends_at])

  if (loading) return <div style={{ ...cardStyle, color: 'var(--text-muted)', fontSize: 13 }}>加载中…</div>
  if (error) return <div style={{ ...cardStyle, color: 'var(--text-muted)', fontSize: 13 }}>{error}</div>
  if (!season) {
    return (
      <div style={{ ...cardStyle, textAlign: 'center', padding: '36px 20px' }}>
        <div style={{ fontSize: 28, marginBottom: 8 }}>🍂</div>
        <div style={{ color: 'var(--text-secondary)', fontSize: 14 }}>当前无进行中的赛季</div>
        <div style={{ color: 'var(--text-muted)', fontSize: 12, marginTop: 6 }}>新赛季开启后将在这里公布，敬请期待。</div>
      </div>
    )
  }
  return (
    <div style={cardStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 18, fontWeight: 700, color: 'var(--text-primary)' }}>🏆 {season.title}</span>
        <span style={{
          fontSize: 11, fontWeight: 600, padding: '2px 8px', borderRadius: 10,
          color: season.status === 'active' ? 'var(--accent-green)' : 'var(--text-muted)',
          border: `1px solid ${season.status === 'active' ? '#53d76944' : 'var(--border)'}`,
        }}>
          {SEASON_STATUS_LABEL[season.status] ?? season.status}
        </span>
      </div>
      {season.theme && (
        <div style={{ color: 'var(--text-secondary)', fontSize: 13, marginTop: 8, lineHeight: 1.6 }}>
          主题：{season.theme}
        </div>
      )}
      <div style={{ color: 'var(--text-muted)', fontSize: 12, marginTop: 10 }}>
        {season.ends_at
          ? <>⏳ 距离赛季结束还有 <span style={{ color: 'var(--accent-red)', fontVariantNumeric: 'tabular-nums' }}>{formatCountdown(season.ends_at, now)}</span></>
          : '本赛季暂未设定结束时间'}
      </div>
    </div>
  )
}

// ── Polls ───────────────────────────────────────────────────────────
type PollVoteState = { kind: 'voted'; idx: number } | { kind: 'already' }

function PollCard({ poll }: { poll: PollData }) {
  // my_vote from the server restores the ✓已投 marker across reloads.
  const [state, setState] = useState<PollVoteState | null>(
    poll.my_vote != null ? { kind: 'voted', idx: poll.my_vote } : null,
  )
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const vote = async (idx: number) => {
    if (busy || state) return
    setBusy(true)
    setErr('')
    try {
      await votePoll(poll.id, idx)
      setState({ kind: 'voted', idx })
    } catch (e) {
      const detail = errDetail(e)
      if (detail.includes('already voted')) {
        setState({ kind: 'already' })
      } else {
        setErr(detail)
      }
    } finally {
      setBusy(false)
    }
  }

  const locked = state !== null
  return (
    <div style={cardStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12 }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)', lineHeight: 1.5 }}>
          🗳️ {poll.question}
        </div>
        {state?.kind === 'already' && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>已投过</span>
        )}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
        {poll.options.map((opt, idx) => {
          const chosen = state?.kind === 'voted' && state.idx === idx
          return (
            <button
              key={idx}
              onClick={() => vote(idx)}
              disabled={locked || busy}
              style={{
                textAlign: 'left', padding: '9px 12px', fontSize: 13,
                background: chosen ? '#e9456022' : 'var(--bg-input)',
                color: chosen ? 'var(--accent-red)' : locked ? 'var(--text-muted)' : 'var(--text-primary)',
                border: chosen ? '1px solid var(--accent-red)' : '1px solid var(--border)',
                borderRadius: 'var(--radius)',
                cursor: locked || busy ? 'default' : 'pointer',
              }}
              onMouseEnter={(e) => {
                if (!locked && !busy) e.currentTarget.style.borderColor = 'var(--accent-red)'
              }}
              onMouseLeave={(e) => {
                if (!chosen) e.currentTarget.style.borderColor = 'var(--border)'
              }}
            >
              {opt}{chosen && <span style={{ marginLeft: 8, fontWeight: 600 }}>✓已投</span>}
            </button>
          )
        })}
      </div>
      {err && <div style={{ color: 'var(--accent-red)', fontSize: 12, marginTop: 8 }}>{err}</div>}
      <div style={{ color: 'var(--text-muted)', fontSize: 11, marginTop: 10 }}>
        {poll.closes_at ? `截止时间：${new Date(poll.closes_at).toLocaleString('zh-CN')}` : '长期开放'}
      </div>
    </div>
  )
}

// ── Leaderboard ─────────────────────────────────────────────────────
function truncateId(uid: string): string {
  return uid.length > 12 ? `${uid.slice(0, 8)}…` : uid
}

function LeaderboardRowView({
  rank, userId, name, points, breakdown, isMe,
}: { rank: number; userId: string; name?: string; points: number; breakdown?: Record<string, number>; isMe: boolean }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, padding: '8px 12px',
      borderRadius: 'var(--radius)',
      border: isMe ? '1px solid var(--accent-red)' : '1px solid transparent',
      background: isMe ? '#e9456012' : 'transparent',
    }}>
      <span style={{
        width: 36, fontVariantNumeric: 'tabular-nums', fontWeight: 700, fontSize: 13,
        color: rank <= 3 ? 'var(--accent-red)' : 'var(--text-secondary)',
      }}>
        {rank <= 3 ? ['🥇', '🥈', '🥉'][rank - 1] : `#${rank}`}
      </span>
      <span style={{
        flex: '0 0 110px', fontSize: 12,
        color: isMe ? 'var(--accent-red)' : 'var(--text-primary)',
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }} title={name || userId}>
        {name || truncateId(userId)}{isMe && '（我）'}
      </span>
      <span style={{
        width: 80, textAlign: 'right', fontVariantNumeric: 'tabular-nums',
        fontWeight: 600, fontSize: 13, color: 'var(--accent-green)',
      }}>
        {points} 分
      </span>
      <span style={{ display: 'flex', gap: 6, flexWrap: 'wrap', flex: 1, justifyContent: 'flex-end' }}>
        {breakdown && Object.entries(breakdown).map(([cat, val]) => (
          <span key={cat} style={{
            fontSize: 10, color: 'var(--text-muted)', background: 'var(--bg-input)',
            padding: '2px 7px', borderRadius: 8, whiteSpace: 'nowrap',
          }}>
            {cat} {val}
          </span>
        ))}
      </span>
    </div>
  )
}

function LeaderboardSection() {
  const user = useGameStore((s) => s.user)
  const myId = user?.id ?? ''
  const [lb, setLb] = useState<LeaderboardResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [noSeason, setNoSeason] = useState(false)

  useEffect(() => {
    getSeasonLeaderboard(true)
      .then((r) => setLb(r))
      .catch((e) => {
        if (isNotFound(e)) setNoSeason(true)
        else setErr('排行榜加载失败，请稍后重试')
      })
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '8px 0' }}>加载中…</div>
  if (noSeason) return <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '8px 0' }}>暂无排行榜 — 当前没有进行中的赛季。</div>
  if (err) return <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '8px 0' }}>{err}</div>
  if (!lb || lb.top.length === 0) {
    return <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '8px 0' }}>还没有人上榜，快去参与赛季活动赚取积分吧。</div>
  }

  const myRank = lb.around_me?.my_rank
  const showAroundMe = lb.around_me != null && myRank != null && myRank > 50

  return (
    <div style={{ ...cardStyle, padding: 12 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {lb.top.map((row) => (
          <LeaderboardRowView
            key={row.user_id}
            rank={row.rank}
            userId={row.user_id}
            name={row.name}
            points={row.points}
            breakdown={row.breakdown}
            isMe={row.user_id === myId}
          />
        ))}
        {showAroundMe && lb.around_me && (
          <>
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 12, padding: '6px 0', letterSpacing: 4 }}>
              ······
            </div>
            {lb.around_me.rows.map((row) => (
              <LeaderboardRowView
                key={row.user_id}
                rank={row.rank}
                userId={row.user_id}
                name={row.name}
                points={row.points}
                isMe={row.user_id === myId}
              />
            ))}
          </>
        )}
      </div>
      {myRank != null && (
        <div style={{ color: 'var(--text-muted)', fontSize: 12, marginTop: 10, paddingLeft: 12 }}>
          我的当前排名：<span style={{ color: 'var(--accent-red)', fontWeight: 600 }}>#{myRank}</span>
        </div>
      )}
    </div>
  )
}

// ── Page ────────────────────────────────────────────────────────────
export function SeasonsPage() {
  const [season, setSeason] = useState<SeasonInfo | null>(null)
  const [seasonLoading, setSeasonLoading] = useState(true)
  const [seasonErr, setSeasonErr] = useState('')
  const [polls, setPolls] = useState<PollData[] | null>(null)
  const [pollsErr, setPollsErr] = useState('')

  useEffect(() => {
    getCurrentSeason()
      .then((r) => setSeason(r.season))
      .catch(() => setSeasonErr('赛季信息加载失败，请稍后重试'))
      .finally(() => setSeasonLoading(false))
    getOpenPolls()
      .then((r) => setPolls(r.polls))
      .catch(() => setPollsErr('投票加载失败，请稍后重试'))
  }, [])

  return (
    <>
      <TopNav />
      <div style={{
        marginTop: 'var(--nav-height)', height: 'calc(100vh - var(--nav-height))',
        overflowY: 'auto', background: 'var(--bg-page)',
      }}>
        <div style={{ maxWidth: 860, margin: '0 auto', padding: '24px 16px 48px' }}>
          <SeasonHeader season={season} loading={seasonLoading} error={seasonErr} />

          <SectionTitle>🗳️ 投票</SectionTitle>
          {polls === null && !pollsErr && <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>加载中…</div>}
          {pollsErr && <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>{pollsErr}</div>}
          {polls !== null && polls.length === 0 && (
            <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无进行中的投票。</div>
          )}
          {polls !== null && polls.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {polls.map((p) => <PollCard key={p.id} poll={p} />)}
            </div>
          )}

          <SectionTitle>🏅 排行榜</SectionTitle>
          <LeaderboardSection />
        </div>
      </div>
    </>
  )
}
