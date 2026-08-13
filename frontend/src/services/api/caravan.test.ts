import { afterEach, describe, expect, it, vi } from 'vitest'
import { getCurrentCaravan } from './caravan'
import { API_BASE } from './core'

afterEach(() => vi.unstubAllGlobals())

describe('caravan api client', () => {
  it('loads the full current projection from /caravans/current', async () => {
    const payload = {
      type: 'caravan_state', visit_id: null, world_event_id: null, version: 0,
      phase: null, server_time: '2026-08-12T02:00:00Z', position: null, motion: null,
      summary: { fee_sc: 0, bought: 0, spent_sc: 0, tax_sc: 0, imports_stocked: 0 },
      visible: false,
    }
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getCurrentCaravan()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(`${API_BASE}/caravans/current`, expect.anything())
  })
})
