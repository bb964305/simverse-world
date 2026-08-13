import Phaser from 'phaser'
import type { CaravanState } from '../services/api/caravan'
import { computeCaravanDepth } from './caravanRenderRuntime'
import { TILE_SIZE } from './worldGeometry'

export const MARKET_HALL_BOUNDS = Object.freeze({
  x1: 105,
  y1: 89,
  x2: 119,
  y2: 99,
})

export const MARKET_HALL_CENTER_TILE = Object.freeze({
  x: 112,
  y: 94,
})

// The sign stands north-west of the five-wide loading throat.  Keeping it off
// y=94 leaves the complete avenue -> hall -> parking centreline unobscured.
export const MARKET_HALL_FORECOURT_TILE = Object.freeze({
  x: 104,
  y: 90,
})

export type MarketHallVisualMode = 'closed' | 'preopen' | 'open' | 'closing'

type PennantStyle = 'rolled' | 'half' | 'full' | 'dropped'

export interface MarketHallVisualSpec {
  mode: MarketHallVisualMode
  signText: string
  signFill: string
  signColor: string
  signStroke: string
  signTiltDeg: number
  signPulse: 'idle' | 'warm' | 'busy' | 'closing'
  pennants: PennantStyle
  pennantAlpha: number
  lanternAlpha: number
  glowAlpha: number
  doorGlowAlpha: number
  goodsGroups: number
  packedCrates: number
  particlesEnabled: boolean
}

const HALL_DEPTH = computeCaravanDepth(MARKET_HALL_CENTER_TILE.y)
const HALL_LEFT_PX = MARKET_HALL_BOUNDS.x1 * TILE_SIZE
const HALL_RIGHT_PX = (MARKET_HALL_BOUNDS.x2 + 1) * TILE_SIZE
const HALL_TOP_PX = MARKET_HALL_BOUNDS.y1 * TILE_SIZE
const HALL_BOTTOM_PX = (MARKET_HALL_BOUNDS.y2 + 1) * TILE_SIZE
const HALL_CENTER_PX_Y = MARKET_HALL_CENTER_TILE.y * TILE_SIZE + TILE_SIZE / 2
const FORECOURT_CENTER_PX_X = MARKET_HALL_FORECOURT_TILE.x * TILE_SIZE + TILE_SIZE / 2
const FORECOURT_CENTER_PX_Y = MARKET_HALL_FORECOURT_TILE.y * TILE_SIZE + TILE_SIZE / 2
const SIGN_BASE_Y = FORECOURT_CENTER_PX_Y - 30
const SIGN_BASE_X = FORECOURT_CENTER_PX_X - 8

const GOODS_GROUPS = [
  { x: HALL_LEFT_PX + 46, y: HALL_TOP_PX + 82 },
  { x: HALL_LEFT_PX + 334, y: HALL_TOP_PX + 82 },
  { x: HALL_LEFT_PX + 46, y: HALL_BOTTOM_PX - 66 },
  { x: HALL_LEFT_PX + 334, y: HALL_BOTTOM_PX - 66 },
] as const

const PACKED_CRATES = [
  { x: HALL_LEFT_PX + 174, y: HALL_TOP_PX + 58 },
  { x: HALL_LEFT_PX + 294, y: HALL_TOP_PX + 66 },
  { x: HALL_LEFT_PX + 294, y: HALL_BOTTOM_PX - 58 },
] as const

const LANTERNS = [
  { x: HALL_LEFT_PX + 64, y: HALL_TOP_PX + 48 },
  { x: HALL_RIGHT_PX - 64, y: HALL_TOP_PX + 48 },
  { x: HALL_LEFT_PX + 64, y: HALL_BOTTOM_PX - 48 },
  { x: HALL_RIGHT_PX - 64, y: HALL_BOTTOM_PX - 48 },
] as const

function warmDotTextureKey(): string {
  return 'market-hall-warm-dot'
}

export function marketHallVisualMode(
  snapshot: Pick<CaravanState, 'phase' | 'visible'> | null,
): MarketHallVisualMode {
  if (!snapshot?.visible) return 'closed'
  if (snapshot.phase === 'inbound') return 'preopen'
  if (snapshot.phase === 'trading') return 'open'
  if (snapshot.phase === 'outbound') return 'closing'
  return 'closed'
}

export function marketHallVisualSpec(
  snapshot: Pick<CaravanState, 'phase' | 'visible'> | null,
): MarketHallVisualSpec {
  switch (marketHallVisualMode(snapshot)) {
    case 'preopen':
      return {
        mode: 'preopen',
        signText: '即将开市',
        signFill: '#7a4a19',
        signColor: '#fff4c9',
        signStroke: '#3a2510',
        signTiltDeg: -4,
        signPulse: 'warm',
        pennants: 'half',
        pennantAlpha: 0.75,
        lanternAlpha: 0.72,
        glowAlpha: 0.16,
        doorGlowAlpha: 0.22,
        goodsGroups: 0,
        packedCrates: 1,
        particlesEnabled: false,
      }
    case 'open':
      return {
        mode: 'open',
        signText: '开市中',
        signFill: '#8f2d1f',
        signColor: '#fff2d6',
        signStroke: '#4a120b',
        signTiltDeg: 0,
        signPulse: 'busy',
        pennants: 'full',
        pennantAlpha: 1,
        lanternAlpha: 0.95,
        glowAlpha: 0.3,
        doorGlowAlpha: 0.3,
        goodsGroups: 4,
        packedCrates: 0,
        particlesEnabled: true,
      }
    case 'closing':
      return {
        mode: 'closing',
        signText: '收摊中',
        signFill: '#5f3b20',
        signColor: '#f6ddbf',
        signStroke: '#2c1a0d',
        signTiltDeg: 5,
        signPulse: 'closing',
        pennants: 'dropped',
        pennantAlpha: 0.55,
        lanternAlpha: 0.52,
        glowAlpha: 0.12,
        doorGlowAlpha: 0.14,
        goodsGroups: 1,
        packedCrates: 3,
        particlesEnabled: false,
      }
    case 'closed':
    default:
      return {
        mode: 'closed',
        signText: '闭市',
        signFill: '#3b2f2f',
        signColor: '#efe0bc',
        signStroke: '#1f1717',
        signTiltDeg: 7,
        signPulse: 'idle',
        pennants: 'rolled',
        pennantAlpha: 0,
        lanternAlpha: 0.18,
        glowAlpha: 0,
        doorGlowAlpha: 0,
        goodsGroups: 0,
        packedCrates: 0,
        particlesEnabled: false,
      }
  }
}

export class MarketHallRuntime {
  private readonly scene: Phaser.Scene
  private readonly props: Phaser.GameObjects.Graphics
  private readonly accents: Phaser.GameObjects.Graphics
  private readonly glows: Phaser.GameObjects.Graphics
  private readonly sign: Phaser.GameObjects.Text
  private emitter: Phaser.GameObjects.Particles.ParticleEmitter | null = null
  private currentMode: MarketHallVisualMode | null = null
  private destroyed = false

  constructor(scene: Phaser.Scene) {
    this.scene = scene
    this.props = scene.add.graphics().setDepth(HALL_DEPTH + 0.01)
    this.accents = scene.add.graphics().setDepth(HALL_DEPTH + 0.02)
    this.glows = scene.add.graphics().setDepth(HALL_DEPTH + 0.015)
    this.sign = scene.add.text(SIGN_BASE_X, SIGN_BASE_Y, '', {
      fontSize: '10px',
      fontStyle: 'bold',
      padding: { x: 6, y: 4 },
    }).setOrigin(0.5).setDepth(3.1)
    this.update(null, 0)
  }

  update(snapshot: Pick<CaravanState, 'phase' | 'visible'> | null, nowMs: number): void {
    if (this.destroyed) return
    const spec = marketHallVisualSpec(snapshot)
    if (this.currentMode !== spec.mode) {
      this.currentMode = spec.mode
      this.redraw(spec)
    }
    this.syncEmitter(spec)
    this.animate(nowMs, spec)
  }

  destroy(): void {
    if (this.destroyed) return
    this.destroyed = true
    this.emitter?.destroy()
    this.emitter = null
    this.props.destroy()
    this.accents.destroy()
    this.glows.destroy()
    this.sign.destroy()
    const warmDot = warmDotTextureKey()
    if (this.scene.textures.exists(warmDot)) this.scene.textures.remove(warmDot)
  }

  private redraw(spec: MarketHallVisualSpec): void {
    this.props.clear()
    this.accents.clear()
    this.glows.clear()

    this.drawForecourtBoard(spec)
    this.drawDoorAccent(spec)
    this.drawAwnings(spec)
    this.drawGoods(spec)
    this.drawPennants(spec)
    this.drawLanterns(spec)
    this.drawGlows(spec)

    this.sign.setText(spec.signText)
    this.sign.setBackgroundColor(spec.signFill)
    this.sign.setColor(spec.signColor)
    this.sign.setStroke(spec.signStroke, 2)
  }

  private drawForecourtBoard(spec: MarketHallVisualSpec): void {
    const boardX = FORECOURT_CENTER_PX_X - 10
    const boardY = FORECOURT_CENTER_PX_Y + 16
    this.props.fillStyle(0x4a2b16, spec.mode === 'closed' ? 0.95 : 0.7)
    this.props.fillRect(boardX - 3, boardY - 12, 6, 30)
    this.props.fillStyle(0x70452a, spec.mode === 'closed' ? 1 : 0.85)
    this.props.fillRoundedRect(boardX - 20, boardY - 24, 40, 18, 4)
    this.props.lineStyle(2, 0x24160d, 0.9)
    this.props.strokeRoundedRect(boardX - 20, boardY - 24, 40, 18, 4)
    if (spec.mode === 'closed' || spec.mode === 'closing') {
      this.props.lineStyle(2, 0xd2b482, 0.8)
      this.props.beginPath()
      this.props.moveTo(boardX - 12, boardY - 18)
      this.props.lineTo(boardX + 12, boardY - 12)
      this.props.strokePath()
    }
  }

  private drawDoorAccent(spec: MarketHallVisualSpec): void {
    const doorX = HALL_LEFT_PX + 4
    const doorY = HALL_CENTER_PX_Y - 80
    const alpha = spec.mode === 'closed' ? 0.3 : 0.8
    this.accents.lineStyle(4, 0x4f3018, alpha)
    this.accents.strokeRect(doorX, doorY, 12, 160)
    this.accents.lineStyle(2, 0xe7c88a, 0.28 + spec.doorGlowAlpha)
    this.accents.strokeRect(doorX + 2, doorY + 4, 8, 152)
  }

  private drawAwnings(spec: MarketHallVisualSpec): void {
    const awningBases = [
      { x: HALL_LEFT_PX + 32, y: HALL_TOP_PX + 62 },
      { x: HALL_LEFT_PX + 320, y: HALL_TOP_PX + 62 },
      { x: HALL_LEFT_PX + 32, y: HALL_BOTTOM_PX - 64 },
      { x: HALL_LEFT_PX + 320, y: HALL_BOTTOM_PX - 64 },
    ] as const
    const extension = spec.pennants === 'full' ? 30 : spec.pennants === 'half' ? 18 : spec.pennants === 'dropped' ? 10 : 0
    for (const { x, y } of awningBases) {
      this.props.fillStyle(0x5f2d1f, 0.95)
      this.props.fillRect(x, y, 128, 10)
      this.props.fillStyle(0xf7e2bb, 0.92)
      this.props.fillRect(x, y + 10, 128, 6)
      if (extension <= 0) continue
      this.props.fillStyle(spec.mode === 'closing' ? 0xb35a45 : 0xd97055, 0.9)
      this.props.beginPath()
      this.props.moveTo(x + 4, y + 16)
      this.props.lineTo(x + 124, y + 16)
      this.props.lineTo(x + 124 - extension / 2, y + 16 + extension)
      this.props.lineTo(x + extension / 2, y + 16 + extension)
      this.props.closePath()
      this.props.fillPath()
      if (spec.mode === 'closing') {
        this.props.lineStyle(2, 0x6e2c23, 0.55)
        this.props.beginPath()
        this.props.moveTo(x + 4, y + 18)
        this.props.lineTo(x + 124, y + 18 + Math.floor(extension / 3))
        this.props.strokePath()
      }
    }
  }

  private drawGoods(spec: MarketHallVisualSpec): void {
    const groupCount = Math.min(spec.goodsGroups, GOODS_GROUPS.length)
    for (let index = 0; index < groupCount; index += 1) {
      const base = GOODS_GROUPS[index]
      this.drawGoodsGroup(base.x, base.y, index)
    }
    const crateCount = Math.min(spec.packedCrates, PACKED_CRATES.length)
    for (let index = 0; index < crateCount; index += 1) {
      const base = PACKED_CRATES[index]
      this.drawPackedCrate(base.x, base.y, spec.mode === 'preopen' ? 0.72 : 0.92)
    }
  }

  private drawGoodsGroup(x: number, y: number, variant: number): void {
    const palettes = [
      [0xba6a3a, 0xd8c46b, 0x7f4fd5],
      [0x4c8f69, 0xc46b5c, 0xd8c46b],
      [0xb85c9b, 0x4d82c2, 0xd3a34e],
      [0xc95a4b, 0x7fc44f, 0x3f6ca6],
    ] as const
    const [crate, cloth, accent] = palettes[variant % palettes.length]
    this.props.fillStyle(0x5a381f, 0.98)
    this.props.fillRect(x, y, 22, 16)
    this.props.fillRect(x + 30, y + 4, 18, 12)
    this.props.fillStyle(crate, 1)
    this.props.fillRect(x + 2, y + 2, 18, 12)
    this.props.fillRect(x + 32, y + 6, 14, 8)
    this.props.fillStyle(cloth, 1)
    this.props.fillRect(x + 10, y - 10, 16, 10)
    this.props.fillStyle(accent, 1)
    this.props.fillRect(x + 28, y - 8, 18, 8)
    this.props.lineStyle(2, 0x23160d, 0.7)
    this.props.strokeRect(x + 2, y + 2, 18, 12)
    this.props.strokeRect(x + 32, y + 6, 14, 8)
  }

  private drawPackedCrate(x: number, y: number, alpha: number): void {
    this.props.fillStyle(0x6e4826, alpha)
    this.props.fillRect(x, y, 20, 14)
    this.props.fillRect(x + 24, y + 2, 16, 12)
    this.props.lineStyle(2, 0x2c1d10, alpha)
    this.props.strokeRect(x, y, 20, 14)
    this.props.strokeRect(x + 24, y + 2, 16, 12)
  }

  private drawPennants(spec: MarketHallVisualSpec): void {
    if (spec.pennants === 'rolled' || spec.pennantAlpha <= 0) return
    const strings = spec.pennants === 'full'
      ? [
          { x1: HALL_LEFT_PX + 82, y1: HALL_TOP_PX + 42, x2: HALL_RIGHT_PX - 82, y2: HALL_TOP_PX + 50, count: 8 },
          { x1: HALL_LEFT_PX + 98, y1: HALL_BOTTOM_PX - 64, x2: HALL_RIGHT_PX - 98, y2: HALL_BOTTOM_PX - 54, count: 6 },
        ]
      : spec.pennants === 'half'
        ? [{ x1: HALL_LEFT_PX + 96, y1: HALL_TOP_PX + 44, x2: HALL_RIGHT_PX - 128, y2: HALL_TOP_PX + 52, count: 5 }]
        : [{ x1: HALL_LEFT_PX + 112, y1: HALL_TOP_PX + 48, x2: HALL_RIGHT_PX - 136, y2: HALL_TOP_PX + 66, count: 4 }]
    const colors = [0xd65353, 0xe7b751, 0x3f86d9, 0x58a764]
    this.accents.lineStyle(2, 0x4a2d1b, spec.pennantAlpha)
    for (const string of strings) {
      this.accents.beginPath()
      this.accents.moveTo(string.x1, string.y1)
      this.accents.lineTo(string.x2, string.y2)
      this.accents.strokePath()
      for (let index = 0; index < string.count; index += 1) {
        const t = index / Math.max(1, string.count - 1)
        const px = Phaser.Math.Linear(string.x1, string.x2, t)
        const py = Phaser.Math.Linear(string.y1, string.y2, t)
        const drop = spec.pennants === 'dropped' ? 16 : spec.pennants === 'half' ? 12 : 18
        this.accents.fillStyle(colors[index % colors.length], spec.pennantAlpha)
        this.accents.beginPath()
        this.accents.moveTo(px - 7, py + 2)
        this.accents.lineTo(px + 7, py + 2)
        this.accents.lineTo(px, py + drop)
        this.accents.closePath()
        this.accents.fillPath()
      }
    }
  }

  private drawLanterns(spec: MarketHallVisualSpec): void {
    if (spec.lanternAlpha <= 0) return
    this.accents.lineStyle(2, 0x50321b, spec.lanternAlpha)
    for (const { x, y } of LANTERNS) {
      this.accents.beginPath()
      this.accents.moveTo(x, y - 16)
      this.accents.lineTo(x, y)
      this.accents.strokePath()
      this.accents.fillStyle(0xf0b95a, spec.lanternAlpha)
      this.accents.fillCircle(x, y + 6, 7)
      this.accents.fillStyle(0x6b311d, spec.lanternAlpha)
      this.accents.fillRect(x - 4, y + 12, 8, 5)
    }
  }

  private drawGlows(spec: MarketHallVisualSpec): void {
    if (spec.glowAlpha > 0) {
      this.glows.fillStyle(0xffd07a, spec.glowAlpha)
      for (const { x, y } of LANTERNS) {
        this.glows.fillCircle(x, y + 8, 22)
      }
      this.glows.fillCircle(HALL_LEFT_PX + 154, HALL_CENTER_PX_Y - 58, 20)
      this.glows.fillCircle(HALL_RIGHT_PX - 90, HALL_CENTER_PX_Y + 58, 20)
    }
    if (spec.doorGlowAlpha > 0) {
      this.glows.fillStyle(0xffdd9a, spec.doorGlowAlpha)
      this.glows.fillEllipse(HALL_LEFT_PX + 18, HALL_CENTER_PX_Y, 42, 152)
    }
  }

  private animate(nowMs: number, spec: MarketHallVisualSpec): void {
    const seconds = nowMs / 1000
    const pulse = spec.signPulse === 'busy'
      ? 0.92 + Math.sin(seconds * 5.2) * 0.08
      : spec.signPulse === 'warm'
        ? 0.88 + Math.sin(seconds * 3.8) * 0.07
        : spec.signPulse === 'closing'
          ? 0.82 + Math.sin(seconds * 2.3) * 0.05
          : 0.96
    const signYOffset = spec.signPulse === 'idle' ? 0 : Math.round(Math.sin(seconds * 3.1) * 2)
    this.sign
      .setPosition(SIGN_BASE_X, SIGN_BASE_Y + signYOffset)
      .setAngle(spec.signTiltDeg)
      .setAlpha(pulse)
      .setScale(spec.mode === 'open' ? 1.03 : 1)
    this.glows.setAlpha(spec.glowAlpha > 0 ? pulse : 1)
    this.accents.setAlpha(spec.mode === 'closed' ? 1 : 0.94 + Math.sin(seconds * 4.1) * 0.04)
  }

  private syncEmitter(spec: MarketHallVisualSpec): void {
    if (!spec.particlesEnabled) {
      this.emitter?.destroy()
      this.emitter = null
      return
    }
    if (this.emitter) return
    this.emitter = this.scene.add.particles(0, 0, this.ensureWarmDotTexture(), {
      x: { min: HALL_LEFT_PX + 48, max: HALL_RIGHT_PX - 48 },
      y: { min: HALL_TOP_PX + 56, max: HALL_BOTTOM_PX - 56 },
      speedX: { min: -10, max: 10 },
      speedY: { min: -28, max: -8 },
      lifespan: { min: 500, max: 1000 },
      scale: { start: 1.2, end: 0.2 },
      alpha: { start: 0.8, end: 0 },
      frequency: 110,
      quantity: 1,
      maxAliveParticles: 18,
      blendMode: 'ADD',
    }).setDepth(HALL_DEPTH + 0.018)
  }

  private ensureWarmDotTexture(): string {
    const key = warmDotTextureKey()
    if (this.scene.textures.exists(key)) return key
    const graphics = this.scene.make.graphics({}, false)
    graphics.fillStyle(0xffd58a, 1)
    graphics.fillRect(0, 0, 3, 3)
    graphics.generateTexture(key, 3, 3)
    graphics.destroy()
    return key
  }
}
