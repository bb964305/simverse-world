import { useCallback, useEffect, useState } from 'react'
import { getFeed, unfollowResident, type FeedEventData } from '../../services/api'
import { setFollowed } from '../../services/follows'
import { onWSMessage } from '../../services/ws'

// Feed kinds and their payloads (backend feed_service.push call sites):
//   creation           { post_id, title }        bulletin_service.maybe_create_journal_post
//   goal_achieved      { title, verdict }        goal_service (verdict === 'achieved')
//   goal_milestone     { title, verdict }        goal_service
//   personality_shift  { old, new }              personality/evolution.py
//   mood* / debate*    reserved kinds (feed.py model comment) — no push site yet
const KIND_META: { match: (k: string) => boolean; icon: string; label: string }[] = [
  { match: (k) => k === 'creation', icon: '🎨', label: '发布了新创作' },
  { match: (k) => k === 'goal_achieved', icon: '🎯', label: '达成目标' },
  { match: (k) => k === 'goal_milestone', icon: '🚩', label: '目标里程碑' },
  { match: (k) => k.startsWith('mood'), icon: '💫', label: '心情波动' },
  { match: (k) => k.startsWith('personality'), icon: '🧬', label: '人格演化' },
  { match: (k) => k.startsWith('debate'), icon: '⚔️', label: '辩论动态' },
]

function kindMeta(kind: string): { icon: string; label: string } {
  return KIND_META.find((m) => m.match(kind)) ?? { icon: '📌', label: kind }
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
  for (const key of ['text', 'topic', 'mood', 'summary']) {
    const v = payload[key]
    if (typeof v === 'string' && v) return v
  }
  return ''
}

function relativeTime(iso: string | null): string {
  if (!iso) return ''
  // SQLite may hand back naive UTC timestamps — pin them to UTC before parsing.
  const hasTz = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso)
  const ts = new Date(hasTz ? iso : `${iso}Z`).getTime()
  if (Number.isNaN(ts)) return ''
  const diffMin = Math.floor((Date.now() - ts) / 60000)
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin}分钟前`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour}小时前`
  return `${Math.floor(diffHour / 24)}天前`
}

export function FeedList() {
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
      .catch((e: unknown) => { if (!cancelled) setError(e instanceof Error ? e.message : '加载失败') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

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
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }}>动态</h2>
      <div style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 20 }}>
        你关注的居民的最新动向
      </div>

      {loading ? (
        <div style={{ color: 'var(--text-muted)' }}>加载中…</div>
      ) : error ? (
        <div style={{ color: 'var(--accent-red)', fontSize: 13 }}>{error}</div>
      ) : events.length === 0 ? (
        <div style={{
          background: 'var(--bg-card)', border: '1px solid var(--border)', borderRadius: 10,
          padding: '32px 20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13,
        }}>
          还没有关注任何居民 — 在地图上与居民互动时点击关注
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 640 }}>
            {events.map((e) => {
              const meta = kindMeta(e.kind)
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
                      {relativeTime(e.created_at)}
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
                    取消关注
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
              {loadingMore ? '加载中…' : '加载更多'}
            </button>
          )}
        </>
      )}
    </div>
  )
}
