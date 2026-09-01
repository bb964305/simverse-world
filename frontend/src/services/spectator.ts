import { API_BASE } from './api/core'

/**
 * The spectator client intentionally does not use apiFetch. Public viewers must
 * never inherit the signed-in player's bearer token or its 401/logout side
 * effects. A view code is sent exactly once to create a read-only HttpOnly
 * cookie session; subsequent requests carry that cookie plus, when the backend
 * returns one, a short-lived viewer session token sent via the X-Viewer-Session
 * header. The header channel keeps /watch working when the browser drops the
 * cross-site Set-Cookie (Safari/ITP, third-party-cookie blocking). The token
 * lives only in module memory — never in localStorage or the URL.
 */

export type SpectatorActorKind = 'npc' | 'agent' | 'human'

export interface SpectatorActor {
  slug: string
  name: string
  kind: SpectatorActorKind
  status: string
  district: string | null
  tile_x: number | null
  tile_y: number | null
  sprite_key?: string | null
  portrait_url?: string | null
  is_online?: boolean
}

export interface PublicTownCounts {
  residents: number
  agents: number
  humans: number
  online: number
}

export interface PublicTownActivity {
  at: string
  summary: string
  location?: string | null
}

export interface PublicTownSnapshot {
  generated_at: string
  world_time: string | null
  counts: PublicTownCounts
  residents: SpectatorActor[]
  activity?: PublicTownActivity[]
}

export interface ViewerAgent {
  slug: string
  name: string
  status: string
  model_label?: string | null
  current_goal?: string | null
  is_online?: boolean
}

export interface ViewerLocation {
  slug: string
  name: string
}

export interface ViewerNearby {
  residents: SpectatorActor[]
  players: SpectatorActor[]
}

export interface ViewerEvent {
  at: string
  summary: string
}

export interface ViewerSnapshot {
  generated_at: string
  agent: ViewerAgent
  self: SpectatorActor
  nearby: ViewerNearby
  location: ViewerLocation | string | null
  recent_events?: ViewerEvent[]
}

export class SpectatorApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'SpectatorApiError'
    this.status = status
  }
}

let viewerSessionToken: string | null = null

export function clearViewerSessionToken(): void {
  viewerSessionToken = null
}

function viewerHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (viewerSessionToken) headers['X-Viewer-Session'] = viewerSessionToken
  return headers
}

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json() as { detail?: unknown }
    return typeof body.detail === 'string' ? body.detail : ''
  } catch {
    return ''
  }
}

async function spectatorFetch<T>(
  path: string,
  options: RequestInit,
  fallbackMessage: string,
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, options)
  if (!response.ok) {
    const detail = await readError(response)
    throw new SpectatorApiError(response.status, detail || fallbackMessage)
  }
  return response.json() as Promise<T>
}

export function getPublicTownSnapshot(signal?: AbortSignal): Promise<PublicTownSnapshot> {
  return spectatorFetch(
    '/api/v1/public/town/snapshot',
    {
      method: 'GET',
      credentials: 'omit',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      signal,
    },
    document.documentElement.lang === 'en' ? 'Town status is temporarily unavailable' : '暂时无法读取小镇状态',
  )
}

export function createViewerSession(viewToken: string, signal?: AbortSignal): Promise<{ ok: boolean }> {
  return fetch(`${API_BASE}/api/v1/viewer/sessions`, {
    method: 'POST',
    credentials: 'include',
    cache: 'no-store',
    referrerPolicy: 'no-referrer',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ view_token: viewToken }),
    signal,
  }).then(async (response) => {
    if (!response.ok) {
      const detail = await readError(response)
      throw new SpectatorApiError(response.status, detail || (document.documentElement.lang === 'en' ? 'Viewer code is invalid or revoked' : '查看码无效或已撤销'))
    }
    // Primary result is the HttpOnly cookie. The backend may also return a
    // short-lived viewer_session_token for the X-Viewer-Session header channel
    // (cross-site cookie fallback); a 204 or {ok:true} without it stays valid.
    let sessionToken: string | null = null
    try {
      const body = await response.json() as { viewer_session_token?: unknown }
      if (typeof body.viewer_session_token === 'string' && body.viewer_session_token) {
        sessionToken = body.viewer_session_token
      }
    } catch {
      // Empty or non-JSON body: cookie-only session.
    }
    viewerSessionToken = sessionToken
    return { ok: true }
  })
}

export function getViewerSnapshot(signal?: AbortSignal): Promise<ViewerSnapshot> {
  return spectatorFetch(
    '/api/v1/viewer/snapshot',
    {
      method: 'GET',
      credentials: 'include',
      cache: 'no-store',
      headers: viewerHeaders(),
      signal,
    },
    document.documentElement.lang === 'en' ? 'Viewer session has expired' : '查看会话已失效',
  )
}

export function deleteViewerSession(signal?: AbortSignal): Promise<{ ok: boolean }> {
  clearViewerSessionToken()
  return spectatorFetch(
    '/api/v1/viewer/sessions',
    {
      method: 'DELETE',
      credentials: 'include',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      signal,
    },
    document.documentElement.lang === 'en' ? 'Could not end the viewer session' : '无法结束查看会话',
  )
}
