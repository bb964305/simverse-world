import { apiFetch } from './core'
import type { NotificationItem } from '../../stores/gameStore'

// ── Notifications (S4) ──────────────────────────────────────────────

export interface NotificationListResponse {
  notifications: NotificationItem[]
  unread_count: number
  next_cursor: string | null
}

export function getNotifications(opts: { unreadOnly?: boolean; cursor?: string } = {}): Promise<NotificationListResponse> {
  const params = new URLSearchParams()
  if (opts.unreadOnly) params.set('unread_only', 'true')
  if (opts.cursor) params.set('cursor', opts.cursor)
  const qs = params.toString()
  return apiFetch(`/notifications${qs ? `?${qs}` : ''}`)
}

export function markNotificationsRead(ids: string[]): Promise<{ unread_count: number }> {
  return apiFetch('/notifications/read', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })
}

// ── Relationship graph (C2) ─────────────────────────────────────────
export interface GraphNode { slug: string; name: string; portrait_url: string | null; district: string }
export interface GraphEdge { a: string; b: string; strength: number; label: string; mutual: boolean }

export function getRelationshipGraph(minImportance = 0.3): Promise<{ nodes: GraphNode[]; edges: GraphEdge[] }> {
  return apiFetch(`/graph/relationships?min_importance=${minImportance}`)
}

// ── Follow feed (E11) ───────────────────────────────────────────────
export interface FeedEventData {
  id: string
  resident_slug: string
  kind: string
  payload: Record<string, unknown>
  created_at: string | null
}

export interface FeedResponse {
  events: FeedEventData[]
  next_cursor: string | null
}

export function getFeed(cursor?: string): Promise<FeedResponse> {
  return apiFetch(`/feed${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`)
}

export function followResident(slug: string): Promise<{ ok: boolean }> {
  return apiFetch(`/follows/${encodeURIComponent(slug)}`, { method: 'POST' })
}

export function unfollowResident(slug: string): Promise<{ ok: boolean }> {
  return apiFetch(`/follows/${encodeURIComponent(slug)}`, { method: 'DELETE' })
}

// ── Bulletin posts (A4) ─────────────────────────────────────────────
export interface BulletinPostData {
  id: string
  kind: string
  title: string
  content_md: string
  likes: number
  tips_sc: number
  pinned: boolean
  author_resident_id: string | null
  author_name: string | null
  author_portrait: string | null
  created_at: string | null
}

export interface BulletinPostsResponse {
  posts: BulletinPostData[]
  next_cursor: string | null
}

export function getBulletinPosts(opts: { kind?: string; cursor?: string } = {}): Promise<BulletinPostsResponse> {
  const params = new URLSearchParams()
  if (opts.kind) params.set('kind', opts.kind)
  if (opts.cursor) params.set('cursor', opts.cursor)
  const qs = params.toString()
  return apiFetch(`/bulletin/posts${qs ? `?${qs}` : ''}`)
}
