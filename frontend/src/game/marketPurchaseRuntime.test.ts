import { describe, expect, it, vi } from 'vitest'
import type Phaser from 'phaser'
import {
  MARKET_SHOPPER_TILES,
  MarketPurchaseRuntime,
  isMarketTrading,
  marketPurchaseCopy,
  marketPurchaseEventKey,
  marketShopperInitialDelayMs,
  marketShopperSlot,
  parseMarketPurchaseMessage,
} from './marketPurchaseRuntime'

const trading = {
  visit_id: 'visit-1',
  phase: 'trading' as const,
  visible: true,
}

function message(overrides: Record<string, unknown> = {}) {
  return {
    type: 'market_purchase',
    visit_id: 'visit-1',
    resident_slug: 'mei',
    purchase_id: 'purchase-1',
    sequence: 3,
    item_name: '香料',
    quantity: 2,
    amount_sc: 8,
    ...overrides,
  }
}

describe('market purchase protocol', () => {
  it('parses the full frame and remains compatible with omitted optional fields', () => {
    expect(parseMarketPurchaseMessage(message())).toEqual({
      type: 'market_purchase',
      visit_id: 'visit-1',
      resident_slug: 'mei',
      purchase_id: 'purchase-1',
      sequence: 3,
      item_name: '香料',
      quantity: 2,
      amount_sc: 8,
    })
    expect(parseMarketPurchaseMessage({
      type: 'market_purchase', visit_id: 'v', resident_slug: 'r',
    })).toMatchObject({
      purchase_id: null, sequence: null, item_name: null, quantity: 1, amount_sc: null,
    })
  })

  it('rejects malformed identifiers, ordering fields, quantities, and amounts', () => {
    expect(parseMarketPurchaseMessage(message({ visit_id: '' }))).toBeNull()
    expect(parseMarketPurchaseMessage(message({ resident_slug: 7 }))).toBeNull()
    expect(parseMarketPurchaseMessage(message({ sequence: 1.5 }))).toBeNull()
    expect(parseMarketPurchaseMessage(message({ quantity: 0 }))).toBeNull()
    expect(parseMarketPurchaseMessage(message({ amount_sc: Number.NaN }))).toBeNull()
    expect(parseMarketPurchaseMessage(message({ item_name: [] }))).toBeNull()
  })

  it('uses durable ids first, then integer sequence, with readable exact feedback', () => {
    const parsed = parseMarketPurchaseMessage(message())!
    expect(marketPurchaseEventKey(parsed)).toBe('visit-1:id:purchase-1')
    expect(marketPurchaseEventKey({ ...parsed, purchase_id: null })).toBe('visit-1:seq:3')
    expect(marketPurchaseCopy(parsed)).toEqual({
      bubble: '🛍️ 买下 香料 ×2 ✓',
      float: '−8 SC',
    })
  })
})

describe('market shopper contract', () => {
  it('recognizes only the four reserved visitor pockets', () => {
    expect(MARKET_SHOPPER_TILES).toHaveLength(4)
    expect(new Set(MARKET_SHOPPER_TILES.map(({ x, y }) => `${x},${y}`)).size).toBe(4)
    MARKET_SHOPPER_TILES.forEach(({ x, y }, slot) => {
      expect(marketShopperSlot(x, y)).toBe(slot)
    })
    expect(marketShopperSlot(109, 94)).toBeNull()
    expect(marketShopperSlot(114, 94)).toBeNull()
  })

  it('only opens during a visible trading visit and staggers four cues', () => {
    expect(isMarketTrading(trading)).toBe(true)
    expect(isMarketTrading({ ...trading, phase: 'inbound' })).toBe(false)
    expect(isMarketTrading({ ...trading, visible: false })).toBe(false)
    expect(isMarketTrading({ ...trading, visit_id: null })).toBe(false)
    expect([0, 1, 2, 3].map(marketShopperInitialDelayMs)).toEqual([900, 1320, 1740, 2160])
  })
})

function visualObject() {
  const object = {
    setOrigin: vi.fn(),
    setDepth: vi.fn(),
    setPosition: vi.fn(),
    setText: vi.fn(),
    setBackgroundColor: vi.fn(),
    setAlpha: vi.fn(),
    destroy: vi.fn(),
  }
  for (const method of ['setOrigin', 'setDepth', 'setPosition', 'setText', 'setBackgroundColor', 'setAlpha'] as const) {
    object[method].mockReturnValue(object)
  }
  return object
}

describe('MarketPurchaseRuntime lifecycle', () => {
  it('accepts ordered events only for the active visit and clears immediately on closing', () => {
    const texts: ReturnType<typeof visualObject>[] = []
    const ellipses: ReturnType<typeof visualObject>[] = []
    const scene = {
      add: {
        text: vi.fn(() => {
          const object = visualObject()
          texts.push(object)
          return object
        }),
        ellipse: vi.fn(() => {
          const object = visualObject()
          ellipses.push(object)
          return object
        }),
      },
      tweens: { add: vi.fn() },
    } as unknown as Phaser.Scene
    const sprite = { x: 3648, y: 2992, active: true } as unknown as Phaser.GameObjects.Sprite
    const runtime = new MarketPurchaseRuntime(scene)
    runtime.registerResident('mei', sprite, 114, 93)

    runtime.update({ ...trading, phase: 'inbound' }, 0)
    expect(texts).toHaveLength(0)
    expect(runtime.handleMessage(message(), 10)).toBe(false)

    runtime.update(trading, 100)
    expect(texts).toHaveLength(0)
    runtime.update(trading, 1000)
    expect(texts).toHaveLength(1)
    expect(ellipses).toHaveLength(1)
    expect(texts[0].setText).not.toHaveBeenCalledWith(expect.stringContaining('成交'))
    expect(runtime.handleMessage(message({ visit_id: 'other' }), 1010)).toBe(false)
    expect(runtime.handleMessage(message({ purchase_id: null, sequence: 3 }), 1020)).toBe(true)
    expect(runtime.handleMessage(message({ purchase_id: null, sequence: 3 }), 1030)).toBe(false)
    expect(runtime.handleMessage(message({ purchase_id: null, sequence: 2 }), 1040)).toBe(false)
    expect(runtime.handleMessage(message({ purchase_id: null, sequence: 4 }), 1050)).toBe(true)

    runtime.update({ ...trading, phase: 'outbound' }, 1060)
    expect(texts[0].destroy).toHaveBeenCalledTimes(1)
    expect(ellipses[0].destroy).toHaveBeenCalledTimes(1)
    expect(runtime.handleMessage(message({ sequence: 5 }), 1070)).toBe(false)

    runtime.destroy()
    runtime.destroy()
  })
})
