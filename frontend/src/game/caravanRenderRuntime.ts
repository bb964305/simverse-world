import type { CaravanPose } from '../services/caravanProjection'
import { MAP_TILES_H, TILE_SIZE } from './worldGeometry'

export type CaravanVisualMode = 'convoy' | 'stall'

export interface CaravanWorldPlacement {
  pixelX: number
  pixelY: number
  depth: number
}

const NUDGE_SCALE_PX = 4
const MAX_NUDGE_PX = 8
const STALL_NUDGE_DAMPING = 0.75

const SIDE_PROBES = {
  west: [
    { dx: -1, dy: 0, weight: 1 },
    { dx: -2, dy: 0, weight: 0.6 },
    { dx: -1, dy: -1, weight: 0.45 },
    { dx: -1, dy: 1, weight: 0.45 },
  ],
  east: [
    { dx: 1, dy: 0, weight: 1 },
    { dx: 2, dy: 0, weight: 0.6 },
    { dx: 1, dy: -1, weight: 0.45 },
    { dx: 1, dy: 1, weight: 0.45 },
  ],
  north: [
    { dx: 0, dy: -1, weight: 1 },
    { dx: 0, dy: -2, weight: 0.6 },
    { dx: -1, dy: -1, weight: 0.45 },
    { dx: 1, dy: -1, weight: 0.45 },
  ],
  south: [
    { dx: 0, dy: 1, weight: 1 },
    { dx: 0, dy: 2, weight: 0.6 },
    { dx: -1, dy: 1, weight: 0.45 },
    { dx: 1, dy: 1, weight: 0.45 },
  ],
} as const

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

export function caravanTileKey(x: number, y: number): string {
  return `${x},${y}`
}

function sidePressure(
  anchorX: number,
  anchorY: number,
  probes: ReadonlyArray<{ dx: number; dy: number; weight: number }>,
  blockedTiles: ReadonlySet<string>,
): number {
  let pressure = 0
  for (const { dx, dy, weight } of probes) {
    if (blockedTiles.has(caravanTileKey(anchorX + dx, anchorY + dy))) pressure += weight
  }
  return pressure
}

export function computeCaravanVisualOffset(
  pose: Pick<CaravanPose, 'tileX' | 'tileY'>,
  mode: CaravanVisualMode,
  blockedTiles: ReadonlySet<string>,
): { x: number; y: number } {
  if (blockedTiles.size === 0) return { x: 0, y: 0 }

  const anchorX = Math.round(pose.tileX)
  const anchorY = Math.round(pose.tileY)
  const west = sidePressure(anchorX, anchorY, SIDE_PROBES.west, blockedTiles)
  const east = sidePressure(anchorX, anchorY, SIDE_PROBES.east, blockedTiles)
  const north = sidePressure(anchorX, anchorY, SIDE_PROBES.north, blockedTiles)
  const south = sidePressure(anchorX, anchorY, SIDE_PROBES.south, blockedTiles)
  const damping = mode === 'stall' ? STALL_NUDGE_DAMPING : 1
  const x = clamp((west - east) * NUDGE_SCALE_PX * damping, -MAX_NUDGE_PX, MAX_NUDGE_PX)
  const y = clamp((north - south) * NUDGE_SCALE_PX * damping, -MAX_NUDGE_PX, MAX_NUDGE_PX)

  return {
    x: Math.abs(x) < 2 ? 0 : x,
    y: Math.abs(y) < 2 ? 0 : y,
  }
}

export function computeCaravanDepth(tileY: number): number {
  const normalized = clamp(tileY, 0, MAP_TILES_H - 1) / (MAP_TILES_H - 1)
  return 0.7 + normalized * 0.5
}

export function resolveCaravanWorldPlacement(
  pose: Pick<CaravanPose, 'tileX' | 'tileY'>,
  mode: CaravanVisualMode,
  blockedTiles: ReadonlySet<string>,
): CaravanWorldPlacement {
  const offset = computeCaravanVisualOffset(pose, mode, blockedTiles)
  return {
    pixelX: pose.tileX * TILE_SIZE + TILE_SIZE / 2 + offset.x,
    pixelY: pose.tileY * TILE_SIZE + TILE_SIZE / 2 + offset.y,
    depth: computeCaravanDepth(pose.tileY),
  }
}
