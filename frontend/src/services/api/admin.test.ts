import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getAdminDashboardHealth,
  getAdminDashboardTrends,
  getAdminEconomySeries,
  getAdminOffices,
  getAdminPolicies,
  getAdminSocialGraph,
} from './admin'
import { API_BASE } from './core'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('admin analytics api client', () => {
  it('uses the dashboard trend contract exposed by the backend', async () => {
    const payload = [{ date: '2026-07-26', users: 3, conversations: 14 }]
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const result = await getAdminDashboardTrends('admin-token')

    expect(result[0]).toEqual(payload[0])
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/admin/dashboard/trends`,
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer admin-token' }),
      }),
    )
  })

  it('preserves timeout status while normalizing health rows by service', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse([
      { service: 'searxng', status: 'timeout', detail: 'Connection timed out' },
      { service: 'llm_api', status: 'ok', detail: null },
    ])))

    const result = await getAdminDashboardHealth('admin-token')

    expect(result.searxng).toBe('timeout')
    expect(result.llm_api).toBe('ok')
    expect(result.details.searxng).toBe('Connection timed out')
  })

  it('requests the selected economy-series window', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ days: 7, series: [] }))
    vi.stubGlobal('fetch', fetchMock)

    await getAdminEconomySeries('admin-token', 7)

    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/admin/economy/series?days=7`,
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer admin-token' }),
      }),
    )
  })

  it.each([
    ['social graph', getAdminSocialGraph, '/admin/social-graph', { nodes: [], edges: [], circles: [] }],
    ['offices', getAdminOffices, '/admin/offices', { offices: [] }],
    ['policies', getAdminPolicies, '/admin/policies', { enabled: false, matrix: {}, policies: [] }],
  ])('connects the %s analytics endpoint', async (_label, request, path, payload) => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    await request('admin-token')

    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}${path}`,
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer admin-token' }),
      }),
    )
  })
})
