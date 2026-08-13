import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CaravanState } from './api/caravan'

vi.mock('./api/caravan', () => ({ getCurrentCaravan: vi.fn() }))

import { getCurrentCaravan } from './api/caravan'
import {
  EMPTY_CARAVAN_PROJECTION,
  caravanBannerText,
  caravanRenderMode,
  convergeCaravanState,
  getCaravanProjection,
  parseCaravanState,
  projectCaravanPose,
  reduceCaravanProjection,
  refreshCaravanProjection,
  resetCaravanProjection,
  subscribeCaravanProjection,
} from './caravanProjection'

const SERVER_TIME = '2026-08-12T02:00:00.000Z'

function state(overrides: Partial<CaravanState> = {}): CaravanState {
  return {
    type: 'caravan_state',
    visit_id: 'visit-a',
    world_event_id: 'market-a',
    version: 1,
    phase: 'inbound',
    server_time: SERVER_TIME,
    position: { tile_x: 102, tile_y: 121 },
    motion: {
      path: [[102, 121], [102, 119], [100, 119]],
      started_at: SERVER_TIME,
      ends_at: '2026-08-12T02:00:10.000Z',
    },
    summary: { fee_sc: 0, bought: 0, spent_sc: 0, tax_sc: 0, imports_stocked: 0 },
    visible: true,
    ...overrides,
  }
}

beforeEach(() => {
  resetCaravanProjection()
  vi.mocked(getCurrentCaravan).mockReset()
})

describe('caravan snapshot parsing and ordering', () => {
  it('accepts the full REST/WS shape and rejects malformed path coordinates', () => {
    expect(parseCaravanState(state())).toEqual(state())
    expect(parseCaravanState({
      ...state(),
      motion: { path: [[1, 'bad']], started_at: SERVER_TIME, ends_at: SERVER_TIME },
    })).toBeNull()
    expect(parseCaravanState({
      ...state(),
      motion: { path: [[1.5, 2]], started_at: SERVER_TIME, ends_at: SERVER_TIME },
    })).toBeNull()
    expect(parseCaravanState({
      ...state(),
      motion: {
        path: [[1, 2]],
        started_at: '2026-08-12T02:00:01.000Z',
        ends_at: SERVER_TIME,
      },
    })).toBeNull()
    expect(parseCaravanState({ ...state(), visible: true, position: null })).toBeNull()
    expect(parseCaravanState({ ...state(), position: { tile_x: 1.25, tile_y: 2 } })).toBeNull()
  })

  it('treats backend timestamps without a timezone suffix as UTC', () => {
    const naive = state({ server_time: '2026-08-12T02:00:00' })
    const explicit = state({ visit_id: 'visit-b', server_time: SERVER_TIME })
    const current = reduceCaravanProjection(EMPTY_CARAVAN_PROJECTION, naive, 1_000)
    expect(reduceCaravanProjection(current, explicit, 2_000).snapshot?.visit_id).toBe('visit-b')
  })

  it('ignores duplicate and out-of-order versions within one visit', () => {
    const v3 = reduceCaravanProjection(
      EMPTY_CARAVAN_PROJECTION,
      state({ version: 3 }),
      1_000,
    )
    expect(reduceCaravanProjection(v3, state({ version: 3 }), 2_000)).toBe(v3)
    expect(reduceCaravanProjection(v3, state({ version: 2 }), 2_000)).toBe(v3)
  })

  it('orders different visits by server_time and uses visit_id only as a tie-break', () => {
    const current = reduceCaravanProjection(EMPTY_CARAVAN_PROJECTION, state(), 1_000)
    const olderVisit = state({
      visit_id: 'visit-z',
      server_time: '2026-08-12T01:59:59.000Z',
    })
    expect(reduceCaravanProjection(current, olderVisit, 2_000)).toBe(current)

    const newerVisit = state({
      visit_id: 'visit-b',
      server_time: '2026-08-12T02:00:01.000Z',
    })
    expect(reduceCaravanProjection(current, newerVisit, 2_000).snapshot?.visit_id).toBe('visit-b')
  })

  it('lets a canonical REST empty snapshot clear an active visit at the same timestamp', () => {
    const current = reduceCaravanProjection(EMPTY_CARAVAN_PROJECTION, state(), 1_000)
    const empty = state({
      visit_id: null,
      world_event_id: null,
      version: 0,
      phase: null,
      position: null,
      motion: null,
      visible: false,
    })
    expect(reduceCaravanProjection(current, empty, 2_000).snapshot).toEqual(empty)
  })
})

describe('server-timed caravan path projection', () => {
  it('resumes on the correct polyline segment instead of jumping point-to-point', () => {
    const receivedAt = 10_000
    const projection = reduceCaravanProjection(
      EMPTY_CARAVAN_PROJECTION,
      state({
        position: { tile_x: 0, tile_y: 0 },
        motion: {
          path: [[0, 0], [2, 0], [2, 3]],
          started_at: SERVER_TIME,
          ends_at: '2026-08-12T02:00:10.000Z',
        },
      }),
      receivedAt,
    )
    const pose = projectCaravanPose(projection, receivedAt + 5_000)
    expect(pose).toMatchObject({ tileX: 2, tileY: 0.5, direction: 'down', moving: true })
  })

  it('uses the authoritative position for trading and marks terminal states hidden', () => {
    const trading = state({ phase: 'trading', motion: null, position: { tile_x: 109, tile_y: 94 } })
    const projection = reduceCaravanProjection(EMPTY_CARAVAN_PROJECTION, trading, 1_000)
    expect(projectCaravanPose(projection, 9_000)).toMatchObject({ tileX: 109, tileY: 94, moving: false })
    expect(caravanRenderMode(trading)).toBe('stall')
    expect(caravanBannerText(trading)).toBe('商队已在集市大厅开摊')

    const departed = state({ phase: 'departed', visible: false, motion: null })
    expect(caravanRenderMode(departed)).toBe('hidden')
    expect(caravanBannerText(departed)).toBeNull()
  })

  it('faces the south-gate convoy into town while it waits', () => {
    const waiting = state({ phase: 'waiting', motion: null })
    const projection = reduceCaravanProjection(EMPTY_CARAVAN_PROJECTION, waiting, 1_000)
    expect(projectCaravanPose(projection, 1_000)).toMatchObject({ direction: 'up', moving: false })
  })
})

describe('REST/WS convergence and cleanup', () => {
  it('does not let a slow reconnect GET replace a newer WS frame', async () => {
    convergeCaravanState(state({ version: 2 }), 1_000)
    vi.mocked(getCurrentCaravan).mockResolvedValue(state({ version: 1 }))
    await refreshCaravanProjection()
    expect(getCaravanProjection().snapshot?.version).toBe(2)
  })

  it('accepts a newer reconnect GET and deduplicates concurrent refreshes', async () => {
    convergeCaravanState(state({ version: 1 }), 1_000)
    vi.mocked(getCurrentCaravan).mockResolvedValue(state({ version: 3 }))
    await Promise.all([refreshCaravanProjection(), refreshCaravanProjection()])
    expect(getCurrentCaravan).toHaveBeenCalledTimes(1)
    expect(getCaravanProjection().snapshot?.version).toBe(3)
  })

  it('notifies subscribers with a hidden terminal snapshot so the scene can destroy the view', () => {
    const modes: string[] = []
    const unsubscribe = subscribeCaravanProjection(({ snapshot }) => modes.push(caravanRenderMode(snapshot)))
    convergeCaravanState(state({ phase: 'trading', motion: null }), 1_000)
    convergeCaravanState(state({ version: 2, phase: 'departed', visible: false, motion: null }), 2_000)
    unsubscribe()
    expect(modes).toEqual(['hidden', 'stall', 'hidden'])
  })

  it('does not resurrect a projection when an in-flight refresh resolves after reset', async () => {
    let resolve!: (snapshot: CaravanState) => void
    vi.mocked(getCurrentCaravan).mockReturnValue(new Promise((done) => { resolve = done }))
    const refresh = refreshCaravanProjection()
    resetCaravanProjection()
    resolve(state())
    await refresh
    expect(getCaravanProjection()).toBe(EMPTY_CARAVAN_PROJECTION)
  })
})
