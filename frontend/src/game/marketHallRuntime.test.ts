import { describe, expect, it, vi } from 'vitest'
import type { CaravanState } from '../services/api/caravan'

// The spec helpers are pure.  Mock Phaser's one runtime math helper so jsdom
// never initializes CanvasFeatures (which requires a native canvas context).
vi.mock('phaser', () => ({
  default: {
    Math: { Linear: (start: number, end: number, t: number) => start + (end - start) * t },
  },
}))
import {
  MARKET_HALL_BOUNDS,
  MARKET_HALL_CENTER_TILE,
  MARKET_HALL_FORECOURT_TILE,
  marketHallVisualMode,
  marketHallVisualSpec,
} from './marketHallRuntime'

const SERVER_TIME = '2026-08-12T02:00:00.000Z'

function state(overrides: Partial<CaravanState> = {}): CaravanState {
  return {
    type: 'caravan_state',
    visit_id: 'visit-market',
    world_event_id: 'event-market',
    version: 1,
    phase: 'inbound',
    server_time: SERVER_TIME,
    position: { tile_x: 102, tile_y: 121 },
    motion: null,
    summary: { fee_sc: 0, bought: 0, spent_sc: 0, tax_sc: 0, imports_stocked: 0 },
    visible: true,
    ...overrides,
  }
}

describe('market hall runtime spec', () => {
  it('exports the agreed hall geometry for scene and ambience consumers', () => {
    expect(MARKET_HALL_BOUNDS).toEqual({ x1: 105, y1: 89, x2: 119, y2: 99 })
    expect(MARKET_HALL_CENTER_TILE).toEqual({ x: 112, y: 94 })
    expect(MARKET_HALL_FORECOURT_TILE).toEqual({ x: 104, y: 90 })
  })

  it('keeps the hall visibly closed when no renderable caravan exists', () => {
    expect(marketHallVisualMode(null)).toBe('closed')
    expect(marketHallVisualMode(state({ visible: false, phase: 'departed' }))).toBe('closed')
    expect(marketHallVisualSpec(state({ phase: 'waiting' }))).toMatchObject({
      mode: 'closed',
      signText: '闭市',
      pennants: 'rolled',
      particlesEnabled: false,
    })
  })

  it('maps inbound, trading, outbound, and hidden phases to the expected hall states', () => {
    expect(marketHallVisualSpec(state({ phase: 'inbound' }))).toMatchObject({
      mode: 'preopen',
      signText: '即将开市',
      goodsGroups: 0,
      packedCrates: 1,
      pennants: 'half',
      particlesEnabled: false,
    })
    expect(marketHallVisualSpec(state({ phase: 'trading' }))).toMatchObject({
      mode: 'open',
      signText: '开市中',
      goodsGroups: 4,
      packedCrates: 0,
      pennants: 'full',
      particlesEnabled: true,
    })
    expect(marketHallVisualSpec(state({ phase: 'outbound' }))).toMatchObject({
      mode: 'closing',
      signText: '收摊中',
      goodsGroups: 1,
      packedCrates: 3,
      pennants: 'dropped',
      particlesEnabled: false,
    })
    expect(marketHallVisualSpec(state({ visible: false, phase: 'cancelled' }))).toMatchObject({
      mode: 'closed',
      signText: '闭市',
    })
  })

  it('is idempotent for repeated snapshots of the same phase', () => {
    const trading = state({ phase: 'trading', version: 4 })
    expect(marketHallVisualSpec(trading)).toEqual(marketHallVisualSpec(trading))
    expect(marketHallVisualSpec(state({ phase: 'outbound', version: 9 })))
      .toEqual(marketHallVisualSpec(state({ phase: 'outbound', version: 9 })))
  })

  it('produces a stable transition chain for inbound to trading to outbound to hidden', () => {
    const chain = [
      marketHallVisualMode(state({ phase: 'inbound' })),
      marketHallVisualMode(state({ phase: 'trading' })),
      marketHallVisualMode(state({ phase: 'outbound' })),
      marketHallVisualMode(state({ visible: false, phase: 'departed' })),
    ]
    expect(chain).toEqual(['preopen', 'open', 'closing', 'closed'])
  })
})
