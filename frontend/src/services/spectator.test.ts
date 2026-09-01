import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  clearViewerSessionToken,
  createViewerSession,
  deleteViewerSession,
  getPublicTownSnapshot,
  getViewerSnapshot,
  SpectatorApiError,
} from './spectator'
import { API_BASE } from './api/core'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  localStorage.clear()
  clearViewerSessionToken()
  vi.unstubAllGlobals()
})

describe('spectator API isolation', () => {
  it('never sends a signed-in player bearer token to the public town endpoint', async () => {
    sessionStorage.setItem('token', 'player-control-token')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      generated_at: '2026-08-13T12:00:00Z',
      world_time: null,
      counts: { residents: 0, agents: 0, humans: 0, online: 0 },
      residents: [],
    }))
    vi.stubGlobal('fetch', fetchMock)

    await getPublicTownSnapshot()

    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/v1/public/town/snapshot`,
      expect.objectContaining({ credentials: 'omit', cache: 'no-store' }),
    )
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.headers).not.toHaveProperty('Authorization')
  })

  it('exchanges a view token once in the request body and opts into the viewer cookie', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await createViewerSession('sv_view_secret')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`${API_BASE}/api/v1/viewer/sessions`)
    expect(url).not.toContain('sv_view_secret')
    expect(init).toMatchObject({ method: 'POST', credentials: 'include' })
    expect(JSON.parse(String(init.body))).toEqual({ view_token: 'sv_view_secret' })
  })

  it('reads the private view with only the HttpOnly cookie session', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ generated_at: 'now' }))
    vi.stubGlobal('fetch', fetchMock)

    await getViewerSnapshot()

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.credentials).toBe('include')
    expect(init.method).toBe('GET')
    expect(init.headers).not.toHaveProperty('Authorization')
    expect(init.body).toBeUndefined()
  })

  it('preserves the status on rejected viewer sessions', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'revoked' }, 401)))
    const failure = await getViewerSnapshot().catch((reason) => reason)
    expect(failure).toBeInstanceOf(SpectatorApiError)
    expect(failure).toMatchObject({ status: 401, message: 'revoked' })
  })

  it('ends a viewer session with a cookie-scoped read-only request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    await deleteViewerSession()
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/api/v1/viewer/sessions`,
      expect.objectContaining({ method: 'DELETE', credentials: 'include' }),
    )
  })
})

describe('viewer session header channel (F10)', () => {
  it('keeps viewer_session_token in module memory and sends X-Viewer-Session afterwards', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ ok: true, viewer_session_token: 'sv_sess_abc' }))
      .mockResolvedValueOnce(jsonResponse({ generated_at: 'now' }))
    vi.stubGlobal('fetch', fetchMock)

    await createViewerSession('sv_view_secret')
    await getViewerSnapshot()

    const init = fetchMock.mock.calls[1][1] as RequestInit
    expect(init.headers).toMatchObject({ 'X-Viewer-Session': 'sv_sess_abc' })
    expect(localStorage.length).toBe(0)
  })

  it('tolerates a token-less session response and stays cookie-only', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ generated_at: 'now' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(createViewerSession('sv_view_secret')).resolves.toEqual({ ok: true })
    await getViewerSnapshot()

    const init = fetchMock.mock.calls[1][1] as RequestInit
    expect(init.headers).not.toHaveProperty('X-Viewer-Session')
  })

  it('stops sending the header once the viewer session ends', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ ok: true, viewer_session_token: 'sv_sess_abc' }))
      .mockResolvedValueOnce(jsonResponse({ ok: true }))
      .mockResolvedValueOnce(jsonResponse({ generated_at: 'now' }))
    vi.stubGlobal('fetch', fetchMock)

    await createViewerSession('sv_view_secret')
    await deleteViewerSession()
    await getViewerSnapshot()

    const init = fetchMock.mock.calls[2][1] as RequestInit
    expect(init.headers).not.toHaveProperty('X-Viewer-Session')
  })
})
