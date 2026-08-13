import { describe, expect, it } from 'vitest'
import {
  caravanTileKey,
  computeCaravanDepth,
  computeCaravanVisualOffset,
  resolveCaravanWorldPlacement,
} from './caravanRenderRuntime'

function blockedTiles(...tiles: Array<[number, number]>): Set<string> {
  return new Set(tiles.map(([x, y]) => caravanTileKey(x, y)))
}

describe('caravan render runtime', () => {
  it('nudges the convoy south when roofs or trees crowd the north side of the anchor', () => {
    const offset = computeCaravanVisualOffset(
      { tileX: 90, tileY: 85 },
      'convoy',
      blockedTiles([90, 84], [90, 83], [89, 84]),
    )
    expect(offset.y).toBeGreaterThan(0)
    expect(offset.x).toBe(0)
  })

  it('nudges the convoy east when walls crowd the west side of the anchor', () => {
    const offset = computeCaravanVisualOffset(
      { tileX: 100, tileY: 81 },
      'convoy',
      blockedTiles([99, 81], [98, 81], [99, 80]),
    )
    expect(offset.x).toBeGreaterThan(0)
    expect(offset.y).toBe(0)
  })

  it('keeps the parked stall steadier than the moving convoy at the same risky tile', () => {
    const nearbyWalls = blockedTiles([99, 81], [98, 81], [99, 80])
    const convoy = computeCaravanVisualOffset({ tileX: 100, tileY: 81 }, 'convoy', nearbyWalls)
    const stall = computeCaravanVisualOffset({ tileX: 100, tileY: 81 }, 'stall', nearbyWalls)
    expect(Math.abs(stall.x)).toBeLessThan(Math.abs(convoy.x))
    expect(Math.abs(stall.y)).toBeLessThanOrEqual(Math.abs(convoy.y))
  })

  it('maps the southern entrance in front of actors but lets the plaza sit behind them', () => {
    expect(computeCaravanDepth(100)).toBeGreaterThan(1)
    expect(computeCaravanDepth(57)).toBeLessThan(1)
    expect(computeCaravanDepth(57)).toBeLessThan(2)
  })

  it('combines tile position, nudge, and depth into one placement payload', () => {
    const placement = resolveCaravanWorldPlacement(
      { tileX: 75, tileY: 57 },
      'stall',
      blockedTiles(),
    )
    expect(placement.pixelX).toBe(75 * 32 + 16)
    expect(placement.pixelY).toBe(57 * 32 + 16)
    expect(placement.depth).toBeCloseTo(computeCaravanDepth(57))
  })
})
