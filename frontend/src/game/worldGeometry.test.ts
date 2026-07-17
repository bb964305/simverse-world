import { describe, expect, it } from 'vitest'
import { MAP_TILES_H, MAP_TILES_W, TILE_SIZE, mapHeightForWidth } from './worldGeometry'

describe('world geometry', () => {
  it('matches the expanded town tile grid', () => {
    expect(MAP_TILES_W).toBe(180)
    expect(MAP_TILES_H).toBe(128)
    expect(TILE_SIZE).toBe(32)
  })

  it('preserves the town aspect ratio for minimap sizes', () => {
    expect(mapHeightForWidth(MAP_TILES_W)).toBe(MAP_TILES_H)
    expect(mapHeightForWidth(560)).toBe(398)
  })
})
