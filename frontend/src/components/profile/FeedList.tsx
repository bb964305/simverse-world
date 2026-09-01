import { useCallback, useEffect, useState } from 'react'
import { getFeed, unfollowResident, type FeedEventData } from '../../services/api'
import { setFollowed } from '../../services/follows'
import { onWSMessage } from '../../services/ws'
import { parseUTC } from '../../utils/time'
import { useLocale } from '../../services/locale'

// Feed kinds and their payloads (backend feed_service.push call sites):
//   creation           { post_id, title }        bulletin_service.maybe_create_journal_post
//   goal_achieved      { title, verdict }        goal_service (verdict === 'achieved')
//   goal_milestone     { title, verdict }        goal_service
//   personality_shift  { old, new }              personality/evolution.py
//   mood* / debate*    reserved kinds (feed.py model comment) — no push site yet
const KIND_META: { match: (k: string) => boolean; icon: string; zh: string; en: string }[] = [
  { match: (k) => k === 'creation', icon: '🎨', zh: '发布了新创作', en: 'published a new creation' },
  { match: (k) => k === 'goal_achieved', icon: '🎯', zh: '达成目标', en: 'achieved a goal' },
  { match: (k) => k === 'goal_milestone', icon: '🚩', zh: '目标里程碑', en: 'reached a goal milestone' },
  { match: (k) => k.startsWith('mood'), icon: '💫', zh: '心情波动', en: 'experienced a mood shift' },
  { match: (k) => k.startsWith('personality'), icon: '🧬', zh: '人格演化', en: 'evolved their personality' },
  { match: (k) => k.startsWith('debate'), icon: '⚔️', zh: '辩论动态', en: 'joined a debate' },
  { match: (k) => k === 'wage', icon: '🪙', zh: '领取了工作报酬', en: 'received work credits' },
  { match: (k) => k === 'meal_income', icon: '🍲', zh: '获得了餐饮收入', en: 'received dining income' },
  { match: (k) => k === 'npc_purchase', icon: '🛍️', zh: '完成了一笔镇民交易', en: 'completed a resident trade' },
  { match: (k) => k === 'npc_commission_taken', icon: '🗒️', zh: '接下了一份委托', en: 'accepted a commission' },
  { match: (k) => k === 'npc_commission_done', icon: '✅', zh: '完成了一份委托', en: 'completed a commission' },
  { match: (k) => k === 'caravan_purchase', icon: '🛒', zh: '作品被商队收购', en: 'sold a creation to the caravan' },
]

function kindMeta(kind: string, en: boolean): { icon: string; label: string } {
  const meta = KIND_META.find((m) => m.match(kind))
  return meta ? { icon: meta.icon, label: en ? meta.en : meta.zh } : { icon: '📌', label: kind }
}

function payloadSummary(kind: string, payload: Record<string, unknown>): string {
  if (kind.startsWith('personality') && typeof payload.old === 'string' && typeof payload.new === 'string') {
    return `${payload.old} → ${payload.new}`
  }
  const title = typeof payload.title === 'string' ? payload.title : null
  if (title) {
    if (kind === 'creation') return `《${title}》`
    return title
  }
  const name = typeof payload.name === 'string' ? payload.name : null
  const amount = typeof payload.amount === 'number' ? payload.amount
    : typeof payload.earned === 'number' ? payload.earned
      : typeof payload.price === 'number' ? payload.price : null
  if (name && amount !== null) return `「${name}」· ${amount} SC`
  if (name) return `「${name}」`
  if (amount !== null) return `${amount} SC`
  for (const key of ['text', 'topic', 'mood', 'summary']) {
    const v = payload[key]
    if (typeof v === 'string' && v) return v
  }
  return ''
}

function relativeTime(iso: string | null, en: boolean): string {
  if (!iso) return ''
  // 后端 naive-UTC isoformat —— 统一走 parseUTC 补 Z（原地内联版收编进 utils/time）
  const ts = parseUTC(iso).getTime()
  if (Number.isNaN(ts)) return ''
  const diffMin = Math.floor((Date.now() - ts) / 60000)
  if (diffMin < 1) return en ? 'just now' : '刚刚'
  if (diffMin < 60) return en ? `${diffMin}m ago` : `${diffMin}分钟前`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return en ? `${diffHour}h ago` : `${diffHour}小时前`
  return en ? `${Math.floor(diffHour / 24)}d ago` : `${Math.floor(diffHour / 24)}天前`
}

export function FeedList() {
  const en = useLocale((state) => state.locale === 'en')
  const [events, setEvents] = useState<FeedEventData[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [unfollowing, setUnfollowing] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getFeed()
      .then((r) => {
        if (cancelled) return
        setEvents(r.events)
        setNextCursor(r.next_cursor)
      })
      .catch((e: unknown) => { if (!cancelled) setError(e instanceof Error ? e.message : (en ? 'Could not load feed' : '加载失败')) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [en])

  // Live: a followed resident just did something — prepend a synthesized row.
  useEffect(() => {
    return onWSMessage((data) => {
      if (data.type !== 'feed_event' || typeof data.resident_slug !== 'string') return
      setEvents((prev) => [{
        id: `live-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        resident_slug: data.resident_slug as string,
        kind: (data.kind as string) || 'unknown',
        payload: (data.payload as Record<string, unknown>) || {},
        created_at: new Date().toISOString(),
      }, ...prev])
    })
  }, [])

  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return
    setLoadingMore(true)
    try {
      const r = await getFeed(nextCursor)
      setEvents((prev) => [...prev, ...r.events])
      setNextCursor(r.next_cursor)
    } catch {
      // keep what we have; button stays for retry
    } finally {
      setLoadingMore(false)
    }
  }, [nextCursor, loadingMore])

  const handleUnfollow = useCallback(async (slug: string) => {
    setUnfollowing(slug)
    try {
      await unfollowResident(slug)
      setFollowed(slug, false)
      setEvents((prev) => prev.filter((e) => e.resident_slug !== slug))
    } catch {
      // ignore; row stays and user can retry
    } finally {
      setUnfollowing(null)
    }
  }, [])

  return (
    <div>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }}>{en ? 'Activity' : '动态'}</h2>
      <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 20 }}>
        {en ? 'Recent activity from residents you follow' : '你关注的居民的最新动向'}
      </div>

      {loading ? (
        <div style={{ color: 'var(--text-muted)' }}>{en ? 'Loading…' : '加载中…'}</div>
      ) : error ? (
        <div style={{ color: 'var(--accent-red)', fontSize: 13 }}>{error}</div>
      ) : events.length === 0 ? (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10,
          padding: '32px 20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13,
        }}>
          {en ? 'No activity yet. Follow residents from their map interaction panel to see their creations and changes here.' : '还没有新动态 — 关注居民后，他们的创作和变化会出现在这里（在地图上与居民互动时点击关注）'}
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 640 }}>
            {events.map((e) => {
              const meta = kindMeta(e.kind, en)
              const summary = payloadSummary(e.kind, e.payload)
              return (
                <div key={e.id} style={{
                  background: 'var(--bg-card)', border: '1px solid var(--border)',
                  borderRadius: 10, padding: '12px 16px', display: 'flex', alignItems: 'flex-start', gap: 12,
                }}>
                  <span style={{ fontSize: 20, flexShrink: 0, marginTop: 2 }}>{meta.icon}</span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, display: 'flex', alignItems: 'baseline', gap: 6, flexWrap: 'wrap' }}>
                      <span style={{ fontWeight: 700, color: 'var(--text-primary)' }}>{e.resident_slug}</span>
                      <span style={{ color: 'var(--text-secondary)' }}>{meta.label}</span>
                    </div>
                    {summary && (
                      <div style={{
                        fontSize: 13, color: 'var(--text-secondary)', marginTop: 4, lineHeight: 1.5,
                        overflow: 'hidden', textOverflow: 'ellipsis',
                      }}>{summary}</div>
                    )}
                    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
                      {relativeTime(e.created_at, en)}
                    </div>
                  </div>
                  <button
                    onClick={() => void handleUnfollow(e.resident_slug)}
                    disabled={unfollowing === e.resident_slug}
                    style={{
                      flexShrink: 0, background: 'transparent', border: '1px solid var(--border)',
                      borderRadius: 6, color: 'var(--text-muted)', fontSize: 11,
                      padding: '4px 8px', cursor: 'pointer',
                      opacity: unfollowing === e.resident_slug ? 0.5 : 1,
                    }}
                    onMouseEnter={(ev) => {
                      ev.currentTarget.style.borderColor = 'var(--accent-red)'
                      ev.currentTarget.style.color = 'var(--accent-red)'
                    }}
                    onMouseLeave={(ev) => {
                      ev.currentTarget.style.borderColor = 'var(--border)'
                      ev.currentTarget.style.color = 'var(--text-muted)'
                    }}
                  >
                    {en ? 'Unfollow' : '取消关注'}
                  </button>
                </div>
              )
            })}
          </div>
          {nextCursor && (
            <button
              onClick={() => void loadMore()}
              disabled={loadingMore}
              style={{
                marginTop: 14, padding: '8px 20px', background: 'var(--bg-input)',
                border: '1px solid var(--border)', borderRadius: 8,
                color: 'var(--text-secondary)', fontSize: 13, cursor: 'pointer',
                opacity: loadingMore ? 0.6 : 1,
              }}
            >
              {loadingMore ? (en ? 'Loading…' : '加载中…') : (en ? 'Load more' : '加载更多')}
            </button>
          )}
        </>
      )}
    </div>
  )
}
