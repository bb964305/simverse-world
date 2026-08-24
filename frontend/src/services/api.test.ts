import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { apiFetch, API_BASE, getMe } from './api'
import { useGameStore } from '../stores/gameStore'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(() => {
  localStorage.clear()
  useGameStore.setState({ user: null, token: null })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('apiFetch', () => {
  it('resolves JSON and hits API_BASE + path', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: 1 }))
    vi.stubGlobal('fetch', fetchMock)

    const out = await apiFetch<{ ok: number }>('/health')
    expect(out.ok).toBe(1)
    expect(fetchMock).toHaveBeenCalledWith(`${API_BASE}/health`, expect.anything())
  })

  it('attaches Bearer token from localStorage when present', async () => {
    localStorage.setItem('token', 'tok-abc')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({}))
    vi.stubGlobal('fetch', fetchMock)

    await apiFetch('/x')
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok-abc')
  })

  it('lets an authorization gate pin /users/me to its current store token', async () => {
    localStorage.setItem('token', 'newer-tab-token')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      id: 'admin-1',
      name: 'Admin',
      email: 'admin@example.com',
      avatar: null,
      soul_coin_balance: 0,
      is_admin: true,
    }))
    vi.stubGlobal('fetch', fetchMock)

    await getMe('guard-token')

    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer guard-token')
  })

  it('401 logs out through the store exactly once and throws', async () => {
    localStorage.setItem('token', 'stale')
    useGameStore.setState({ token: 'stale' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })))

    await expect(apiFetch('/me')).rejects.toThrow('Session expired')
    // store-centralized logout: token gone from both state and localStorage
    expect(useGameStore.getState().token).toBeNull()
    expect(localStorage.getItem('token')).toBeNull()
  })

  it('non-401 error surfaces status and body text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('boom', { status: 500 })))
    await expect(apiFetch('/x')).rejects.toThrow('API 500: boom')
  })

  it('timeout abort maps to a friendly Chinese message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new DOMException('timed out', 'TimeoutError')),
    )
    await expect(apiFetch('/slow')).rejects.toThrow('请求超时')
  })

  it('caller AbortError passes through untouched (component unmount)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new DOMException('aborted', 'AbortError')),
    )
    await expect(apiFetch('/x')).rejects.toMatchObject({ name: 'AbortError' })
  })
})
