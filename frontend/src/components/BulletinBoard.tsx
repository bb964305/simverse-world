import { useState, useEffect, useRef } from 'react'
import { bridge } from '../game/phaserBridge'
import { getBulletinPosts, type BulletinPostData } from '../services/api'
import { parseUTC } from '../utils/time'
import { Pager } from './Pager'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const POSTS_PAGE_SIZE = 10

interface BulletinResident {
  id: string
  slug: string
  name: string
  district: string
  status: string
  heat: number
  tile_x: number
  tile_y: number
  star_rating: number
  token_cost_per_turn: number
  meta_json: { role?: string } | null
}

interface BulletinData {
  hot_residents: BulletinResident[]
  new_residents: BulletinResident[]
  recent_conversations_24h: number
}

const DISTRICT_NAMES: Record<string, string> = {
  engineering: '工程街区',
  product: '产品街区',
  academy: '学院区',
  free: '自由区',
}

// A4 posts tab: kind filter chips ('' = no kind param = all kinds)
const KIND_FILTERS: { value: string; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'journal', label: '日志' },
  { value: 'clue', label: '线索' },
  { value: 'digest', label: '日报' },
  { value: 'notice', label: '公告' },
]

/** Strip common markdown markers for a plain-text preview. */
function stripMd(md: string): string {
  return md
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1') // images → alt text
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1') // links → link text
    .replace(/^#{1,6}\s+/gm, '') // headings
    .replace(/[*_`]/g, '') // emphasis / code markers
    .replace(/\s+/g, ' ')
    .trim()
}

function previewText(md: string): string {
  const text = stripMd(md)
  return text.length > 200 ? `${text.slice(0, 200)}…` : text
}

function relativeTime(iso: string | null): string {
  if (!iso) return ''
  // 后端 naive-UTC isoformat —— 统一走 parseUTC 补 Z（原地内联版收编进 utils/time）
  const ts = parseUTC(iso).getTime()
  if (Number.isNaN(ts)) return ''
  const diffMin = Math.floor((Date.now() - ts) / 60000)
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin}分钟前`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour}小时前`
  return `${Math.floor(diffHour / 24)}天前`
}

export function BulletinBoard() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<BulletinData | null>(null)
  const [loading, setLoading] = useState(false)
  // Posts tab (A4)
  const [tab, setTab] = useState<'plaza' | 'posts'>('plaza')
  const [postKind, setPostKind] = useState('')
  const [posts, setPosts] = useState<BulletinPostData[]>([])
  const [postsCursor, setPostsCursor] = useState<string | null>(null)
  const [postsPage, setPostsPage] = useState(1)
  const [postsLoading, setPostsLoading] = useState(false)
  const [postsLoadingMore, setPostsLoadingMore] = useState(false)
  const [postsError, setPostsError] = useState<string | null>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const unsub1 = bridge.on('bulletin:open', () => {
      bridge.emit('experiment:close')
      setOpen(true)
      void fetchBulletin()
    })
    const unsub2 = bridge.on('bulletin:close', () => setOpen(false))
    return () => {
      unsub1()
      unsub2()
    }
  }, [])

  useEffect(() => {
    if (!open) return
    closeButtonRef.current?.focus()
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  // First page of posts: fetched when the tab is shown or the kind filter
  // changes (filter switch resets the cursor by refetching from the top).
  useEffect(() => {
    if (!open || tab !== 'posts') return
    let cancelled = false
    setPostsLoading(true)
    setPostsError(null)
    setPosts([])
    setPostsCursor(null)
    setPostsPage(1)
    getBulletinPosts(postKind ? { kind: postKind } : {})
      .then((r) => {
        if (cancelled) return
        setPosts(r.posts)
        setPostsCursor(r.next_cursor)
      })
      .catch(() => { if (!cancelled) setPostsError('加载失败，请稍后重试') })
      .finally(() => { if (!cancelled) setPostsLoading(false) })
    return () => { cancelled = true }
  }, [open, tab, postKind])

  const loadMorePosts = async () => {
    if (!postsCursor || postsLoadingMore) return
    setPostsLoadingMore(true)
    try {
      const r = await getBulletinPosts({ ...(postKind ? { kind: postKind } : {}), cursor: postsCursor })
      setPosts((prev) => [...prev, ...r.posts])
      setPostsCursor(r.next_cursor)
    } catch {
      // keep what we have; button stays for retry
    } finally {
      setPostsLoadingMore(false)
    }
  }

  const fetchBulletin = async () => {
    setLoading(true)
    try {
      const resp = await fetch(`${API}/bulletin`)
      if (resp.ok) setData(await resp.json())
    } catch {
      /* ignore */
    } finally {
      setLoading(false)
    }
  }

  const navigateTo = (r: BulletinResident) => {
    bridge.emit('camera:pan', { tile_x: r.tile_x, tile_y: r.tile_y, slug: r.slug })
    setOpen(false)
  }

  if (!open) return null

  return (
    <div
      className="game-modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) setOpen(false) }}
    >
      <section
        className="game-modal-panel game-modal-panel--bulletin"
        role="dialog"
        aria-modal="true"
        aria-labelledby="bulletin-dialog-title"
        style={{ borderColor: '#f59e0b55' }}
      >
      {/* Header */}
      <div className="game-dialog-header" style={{
        padding: '16px 20px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        background: '#f59e0b08',
      }}>
        <div>
          <div id="bulletin-dialog-title" style={{ fontWeight: 800, fontSize: 15, color: '#f59e0b' }}>📋 中央广场公告板</div>
          <div style={{ color: 'var(--text-muted)', fontSize: 11, marginTop: 2 }}>
            {data ? `最近 24 小时：${data.recent_conversations_24h} 次对话` : '加载中...'}
          </div>
        </div>
        <button ref={closeButtonRef} onClick={() => setOpen(false)} className="game-dialog-close" aria-label="关闭公告板">✕</button>
      </div>

      {/* Tabs: 广场 (legacy plaza view) | 帖子 (A4 posts) */}
      <div style={{
        display: 'flex', gap: 4, padding: '8px 20px 0',
        borderBottom: '1px solid var(--border)',
      }}>
        {([['plaza', '广场'], ['posts', '帖子']] as const).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              padding: '6px 12px 8px', fontSize: 13,
              fontWeight: tab === key ? 700 : 500,
              color: tab === key ? '#f59e0b' : 'var(--text-muted)',
              borderBottom: tab === key ? '2px solid #f59e0b' : '2px solid transparent',
              marginBottom: -1,
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'posts' ? (
        <div style={{ padding: '14px 20px 16px' }}>
          {/* Kind filter chips */}
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 14 }}>
            {KIND_FILTERS.map((f) => (
              <button
                key={f.value}
                onClick={() => setPostKind(f.value)}
                style={{
                  padding: '3px 10px', borderRadius: 999, fontSize: 12, cursor: 'pointer',
                  background: postKind === f.value ? '#f59e0b22' : 'var(--bg-input)',
                  border: postKind === f.value ? '1px solid #f59e0b66' : '1px solid var(--border)',
                  color: postKind === f.value ? '#f59e0b' : 'var(--text-muted)',
                  fontWeight: postKind === f.value ? 700 : 500,
                }}
              >
                {f.label}
              </button>
            ))}
          </div>

          {postsLoading ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>加载中...</div>
          ) : postsError ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>{postsError}</div>
          ) : posts.length === 0 ? (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>暂无帖子</div>
          ) : (
            <>
              {posts.slice((postsPage - 1) * POSTS_PAGE_SIZE, postsPage * POSTS_PAGE_SIZE).map((p) => (
                <div key={p.id} style={{
                  padding: '10px 12px', borderRadius: 10, marginBottom: 8,
                  background: p.pinned ? '#f59e0b0d' : 'var(--bg-input)',
                  border: p.pinned ? '1px solid #f59e0b44' : '1px solid var(--border)',
                }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                    {p.pinned && <span style={{ fontSize: 12, flexShrink: 0 }}>📌</span>}
                    <span style={{ fontWeight: 700, fontSize: 13, color: 'var(--text-primary)' }}>{p.title}</span>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 6 }}>
                    {p.author_portrait ? (
                      <img src={p.author_portrait} alt="" style={{
                        width: 24, height: 24, borderRadius: '50%', objectFit: 'cover', flexShrink: 0,
                      }} />
                    ) : (
                      <span style={{
                        width: 24, height: 24, borderRadius: '50%', fontSize: 13, flexShrink: 0,
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        background: 'var(--bg-card)',
                      }}>🤖</span>
                    )}
                    <span style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 600 }}>
                      {p.author_name || '系统'}
                    </span>
                    <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{relativeTime(p.created_at)}</span>
                  </div>
                  {p.content_md && (
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.6, marginTop: 6 }}>
                      {previewText(p.content_md)}
                    </div>
                  )}
                  <div style={{ display: 'flex', gap: 12, marginTop: 6, fontSize: 11, color: 'var(--text-muted)' }}>
                    <span>❤ {p.likes}</span>
                    <span>🪙 {p.tips_sc}</span>
                  </div>
                </div>
              ))}
              {posts.length > POSTS_PAGE_SIZE && (
                <div style={{ marginTop: 12 }}>
                  <Pager
                    page={postsPage}
                    totalPages={Math.ceil(posts.length / POSTS_PAGE_SIZE)}
                    setPage={setPostsPage}
                  />
                </div>
              )}
              {postsCursor && postsPage >= Math.ceil(posts.length / POSTS_PAGE_SIZE) && (
                <button
                  onClick={() => void loadMorePosts()}
                  disabled={postsLoadingMore}
                  style={{
                    display: 'block', margin: '12px auto 0', padding: '6px 18px',
                    background: 'var(--bg-input)', border: '1px solid var(--border)',
                    borderRadius: 8, color: 'var(--text-secondary)', fontSize: 12,
                    cursor: 'pointer', opacity: postsLoadingMore ? 0.6 : 1,
                  }}
                >
                  {postsLoadingMore ? '加载中...' : '加载更多'}
                </button>
              )}
            </>
          )}
        </div>
      ) : loading ? (
        <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)' }}>加载中...</div>
      ) : data && (
        <div style={{ padding: '16px 20px' }}>
          {/* Hot Residents */}
          <div style={{ marginBottom: 20 }}>
            <div style={{ fontWeight: 700, fontSize: 13, color: '#f59e0b', marginBottom: 10 }}>🔥 热门居民</div>
            {data.hot_residents.length === 0 && (
              <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '8px 10px' }}>暂无数据</div>
            )}
            {data.hot_residents.map((r, i) => (
              <div
                key={r.id}
                onClick={() => navigateTo(r)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
                  cursor: 'pointer', borderRadius: 8, marginBottom: 4,
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-input)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <span style={{
                  width: 22, height: 22, borderRadius: '50%', fontSize: 11, fontWeight: 700,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  background: i < 3 ? '#f59e0b22' : 'var(--bg-input)',
                  color: i < 3 ? '#f59e0b' : 'var(--text-muted)',
                }}>{i + 1}</span>
                <div style={{ flex: 1 }}>
                  <span style={{ fontWeight: 600, fontSize: 13 }}>{r.name}</span>
                  <span style={{ color: 'var(--text-muted)', fontSize: 11, marginLeft: 6 }}>
                    {r.meta_json?.role ?? ''} · {DISTRICT_NAMES[r.district] ?? r.district}
                  </span>
                </div>
                <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>🔥 {r.heat}</span>
              </div>
            ))}
          </div>

          {/* New Residents */}
          <div>
            <div style={{ fontWeight: 700, fontSize: 13, color: 'var(--accent-blue)', marginBottom: 10 }}>✨ 最新入住</div>
            {data.new_residents.length === 0 && (
              <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '8px 10px' }}>暂无数据</div>
            )}
            {data.new_residents.map((r) => (
              <div
                key={r.id}
                onClick={() => navigateTo(r)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
                  cursor: 'pointer', borderRadius: 8, marginBottom: 4,
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--bg-input)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              >
                <div style={{ flex: 1 }}>
                  <span style={{ fontWeight: 600, fontSize: 13 }}>{r.name}</span>
                  <span style={{ color: 'var(--text-muted)', fontSize: 11, marginLeft: 6 }}>
                    {r.meta_json?.role ?? ''} · {'⭐'.repeat(r.star_rating)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      </section>
    </div>
  )
}
