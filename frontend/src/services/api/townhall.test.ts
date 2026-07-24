import { describe, it, expect, vi, afterEach } from 'vitest'
import { getTownHallOverview, getMarketDay } from './townhall'
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

describe('townhall api client', () => {
  it('getTownHallOverview hits /townhall/overview and parses JSON', async () => {
    const payload = {
      mayor: { slug: 'zhao', name: '赵启文' },
      duties: [{ key: 'town_clerk', title: '公告与登记处', holder_slug: 'zhao', holder_name: '赵启文' }],
      open_polls: [],
      recent_election: null,
      finances: { npc_default_wage_sc: 5, market_day_discount: 0.9 },
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    const out = await getTownHallOverview()
    expect(out.mayor?.slug).toBe('zhao')
    expect(out.duties[0].key).toBe('town_clerk')
    expect(fetchMock).toHaveBeenCalledWith(`${API_BASE}/townhall/overview`, expect.anything())
  })

  it('getMarketDay hits /townhall/market-day and parses JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ active: true, discount: 0.9, weekday: 5 }))
    vi.stubGlobal('fetch', fetchMock)

    const out = await getMarketDay()
    expect(out.active).toBe(true)
    expect(out.discount).toBe(0.9)
    expect(fetchMock).toHaveBeenCalledWith(`${API_BASE}/townhall/market-day`, expect.anything())
  })
})
