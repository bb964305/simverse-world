import type Phaser from 'phaser'
import type { CaravanState } from '../services/api/caravan'

/**
 * The four collision-open pockets reserved by the server-side market cohort.
 * Keeping this contract explicit prevents shopper feedback from appearing on
 * residents who merely walk past the hall or on top of the caravan aisle.
 */
export const MARKET_SHOPPER_TILES = Object.freeze([
  { x: 114, y: 93 },
  { x: 116, y: 93 },
  { x: 114, y: 95 },
  { x: 116, y: 95 },
] as const)

const SHOPPER_BUBBLE_OFFSETS = Object.freeze([
  { x: -22, y: -66 },
  { x: 22, y: -52 },
  { x: -22, y: -52 },
  { x: 22, y: -66 },
] as const)

export interface MarketPurchaseMessage {
  type: 'market_purchase'
  visit_id: string
  resident_slug: string
  /** Durable id is preferred. Older/demo producers may send sequence instead. */
  purchase_id: string | null
  sequence: number | null
  item_name: string | null
  quantity: number
  amount_sc: number | null
}

export interface MarketPurchaseCopy {
  bubble: string
  float: string
}

type MarketSnapshot = Pick<CaravanState, 'visit_id' | 'phase' | 'visible'>

interface ShopperState {
  sprite: Phaser.GameObjects.Sprite
  tileX: number | null
  tileY: number | null
  slot: number | null
  bubble: Phaser.GameObjects.Text | null
  halo: Phaser.GameObjects.Ellipse | null
  feedbackUntilMs: number
  browsingVisibleAtMs: number
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function optionalString(value: unknown): string | null | undefined {
  if (value === undefined || value === null) return null
  if (!nonEmptyString(value)) return undefined
  return value.trim().slice(0, 40)
}

/** Parse the optional live purchase frame without trusting WS payloads. */
export function parseMarketPurchaseMessage(value: unknown): MarketPurchaseMessage | null {
  if (!record(value) || value.type !== 'market_purchase') return null
  if (!nonEmptyString(value.visit_id) || !nonEmptyString(value.resident_slug)) return null

  const purchaseId = optionalString(value.purchase_id)
  const itemName = optionalString(value.item_name)
  if (purchaseId === undefined || itemName === undefined) return null

  let sequence: number | null = null
  if (value.sequence !== undefined && value.sequence !== null) {
    if (!Number.isInteger(value.sequence) || (value.sequence as number) < 0) return null
    sequence = value.sequence as number
  }

  const quantity = value.quantity === undefined || value.quantity === null ? 1 : value.quantity
  if (!Number.isInteger(quantity) || (quantity as number) <= 0 || (quantity as number) > 99) return null

  let amountSc: number | null = null
  if (value.amount_sc !== undefined && value.amount_sc !== null) {
    if (typeof value.amount_sc !== 'number'
      || !Number.isFinite(value.amount_sc)
      || value.amount_sc < 0) return null
    amountSc = value.amount_sc
  }

  return {
    type: 'market_purchase',
    visit_id: value.visit_id.trim(),
    resident_slug: value.resident_slug.trim(),
    purchase_id: purchaseId,
    sequence,
    item_name: itemName,
    quantity: quantity as number,
    amount_sc: amountSc,
  }
}

export function marketPurchaseEventKey(message: MarketPurchaseMessage): string {
  if (message.purchase_id) return `${message.visit_id}:id:${message.purchase_id}`
  if (message.sequence !== null) return `${message.visit_id}:seq:${message.sequence}`
  return [
    message.visit_id,
    message.resident_slug,
    message.item_name ?? '',
    message.quantity,
    message.amount_sc ?? '',
  ].join(':')
}

export function marketPurchaseCopy(message: MarketPurchaseMessage): MarketPurchaseCopy {
  const item = message.item_name
    ? `${message.item_name}${message.quantity > 1 ? ` ×${message.quantity}` : ''}`
    : '心仪商品'
  return {
    bubble: `🛍️ 买下 ${item} ✓`,
    float: message.amount_sc === null ? '🪙 成交' : `−${message.amount_sc} SC`,
  }
}

export function marketShopperSlot(tileX: number, tileY: number): number | null {
  const slot = MARKET_SHOPPER_TILES.findIndex(({ x, y }) => x === tileX && y === tileY)
  return slot < 0 ? null : slot
}

export function isMarketTrading(snapshot: MarketSnapshot | null): boolean {
  return Boolean(snapshot?.visible && snapshot.phase === 'trading' && snapshot.visit_id)
}

/** Stagger four buyers so their feedback reads as a living market, not a burst. */
export function marketShopperInitialDelayMs(slot: number): number {
  return 900 + Math.max(0, Math.min(3, slot)) * 420
}

/**
 * Cosmetic shopping feedback for real resident sprites.
 *
 * resident_move remains authoritative for who is at the market. While trading,
 * arrival in one of the four reserved pockets shows a staggered browsing
 * bubble. Only a server-authoritative market_purchase frame may show a
 * transaction, item, or amount; no economy state is mutated here.
 */
export class MarketPurchaseRuntime {
  private readonly scene: Phaser.Scene
  private readonly shoppers = new Map<string, ShopperState>()
  private readonly seenPurchaseKeys = new Set<string>()
  private activeVisitId: string | null = null
  private lastSequence = -1
  private destroyed = false

  constructor(scene: Phaser.Scene) {
    this.scene = scene
  }

  registerResident(
    slug: string,
    sprite: Phaser.GameObjects.Sprite,
    tileX: number,
    tileY: number,
  ): void {
    if (this.destroyed) return
    this.shoppers.set(slug, {
      sprite,
      tileX,
      tileY,
      slot: marketShopperSlot(tileX, tileY),
      bubble: null,
      halo: null,
      feedbackUntilMs: 0,
      browsingVisibleAtMs: 0,
    })
  }

  setResidentMoving(slug: string): void {
    const shopper = this.shoppers.get(slug)
    if (!shopper) return
    shopper.tileX = null
    shopper.tileY = null
    shopper.slot = null
    this.clearVisual(shopper)
  }

  setResidentTile(slug: string, tileX: number, tileY: number): void {
    const shopper = this.shoppers.get(slug)
    if (!shopper) return
    shopper.tileX = tileX
    shopper.tileY = tileY
    shopper.slot = marketShopperSlot(tileX, tileY)
    shopper.browsingVisibleAtMs = 0
    if (shopper.slot === null) this.clearVisual(shopper)
  }

  update(snapshot: MarketSnapshot | null, nowMs: number): void {
    if (this.destroyed) return
    const nextVisitId = isMarketTrading(snapshot) ? snapshot!.visit_id : null
    if (nextVisitId !== this.activeVisitId) {
      this.activeVisitId = nextVisitId
      this.seenPurchaseKeys.clear()
      this.lastSequence = -1
      this.clearAllVisuals()
    }
    if (!this.activeVisitId) return

    for (const shopper of this.shoppers.values()) {
      if (shopper.slot === null || !shopper.sprite.active) {
        this.clearVisual(shopper)
        continue
      }
      if (shopper.browsingVisibleAtMs <= 0) {
        shopper.browsingVisibleAtMs = nowMs + marketShopperInitialDelayMs(shopper.slot)
      }
      if (nowMs < shopper.browsingVisibleAtMs) continue
      this.ensureVisual(shopper)
      const bubbleOffset = SHOPPER_BUBBLE_OFFSETS[shopper.slot]
      shopper.bubble?.setPosition(
        shopper.sprite.x + bubbleOffset.x,
        shopper.sprite.y + bubbleOffset.y,
      )
      shopper.halo?.setPosition(shopper.sprite.x, shopper.sprite.y + 9)
      if (shopper.halo) {
        shopper.halo.setAlpha(0.18 + Math.sin(nowMs / 240 + shopper.slot) * 0.06)
      }
      if (shopper.feedbackUntilMs > 0 && nowMs >= shopper.feedbackUntilMs) {
        shopper.feedbackUntilMs = 0
        shopper.bubble?.setText('🛍️ 挑选商品…').setBackgroundColor('#713f12dd')
      }
    }
  }

  handleMessage(value: unknown, nowMs: number): boolean {
    if (this.destroyed) return false
    const message = parseMarketPurchaseMessage(value)
    if (!message || message.visit_id !== this.activeVisitId) return false
    if (message.sequence !== null && message.sequence <= this.lastSequence) return false
    const key = marketPurchaseEventKey(message)
    if (this.seenPurchaseKeys.has(key)) return false
    const shopper = this.shoppers.get(message.resident_slug)
    if (!shopper || shopper.slot === null) return false

    this.seenPurchaseKeys.add(key)
    if (message.sequence !== null) this.lastSequence = message.sequence
    // Bound memory even if a malformed producer emits unbounded unique ids.
    if (this.seenPurchaseKeys.size > 128) {
      const oldest = this.seenPurchaseKeys.values().next().value as string | undefined
      if (oldest) this.seenPurchaseKeys.delete(oldest)
    }
    this.ensureVisual(shopper)
    shopper.browsingVisibleAtMs = nowMs
    this.playFeedback(shopper, marketPurchaseCopy(message), nowMs)
    return true
  }

  destroy(): void {
    if (this.destroyed) return
    this.destroyed = true
    this.clearAllVisuals()
    this.shoppers.clear()
    this.seenPurchaseKeys.clear()
    this.activeVisitId = null
    this.lastSequence = -1
  }

  private ensureVisual(shopper: ShopperState): void {
    if (!shopper.bubble) {
      const bubbleOffset = SHOPPER_BUBBLE_OFFSETS[shopper.slot ?? 0]
      shopper.bubble = this.scene.add.text(
        shopper.sprite.x + bubbleOffset.x,
        shopper.sprite.y + bubbleOffset.y,
        '🛍️ 挑选商品…', {
        fontSize: '11px',
        color: '#fff7d6',
        backgroundColor: '#713f12dd',
        padding: { x: 7, y: 4 },
      }).setOrigin(0.5).setDepth(12)
    }
    if (!shopper.halo) {
      shopper.halo = this.scene.add.ellipse(
        shopper.sprite.x,
        shopper.sprite.y + 9,
        34,
        12,
        0xfbbf24,
        0.2,
      ).setDepth(0.95)
    }
  }

  private playFeedback(shopper: ShopperState, copy: MarketPurchaseCopy, nowMs: number): void {
    shopper.bubble?.setText(copy.bubble).setBackgroundColor('#166534e6')
    shopper.feedbackUntilMs = nowMs + 1700

    const x = shopper.sprite.x
    const y = shopper.sprite.y - 34
    const floating = this.scene.add.text(x, y, copy.float, {
      fontSize: '13px',
      fontStyle: 'bold',
      color: '#fde68a',
      stroke: '#713f12',
      strokeThickness: 3,
    }).setOrigin(0.5).setDepth(14)
    this.scene.tweens.add({
      targets: floating,
      y: y - 38,
      alpha: { from: 1, to: 0 },
      duration: 1250,
      ease: 'Cubic.easeOut',
      onComplete: () => floating.destroy(),
    })
  }

  private clearVisual(shopper: ShopperState): void {
    shopper.bubble?.destroy()
    shopper.halo?.destroy()
    shopper.bubble = null
    shopper.halo = null
    shopper.feedbackUntilMs = 0
    shopper.browsingVisibleAtMs = 0
  }

  private clearAllVisuals(): void {
    for (const shopper of this.shoppers.values()) this.clearVisual(shopper)
  }
}
