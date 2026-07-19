import { describe, expect, it } from 'vitest'
import { LOCATIONS, inclusiveBoundsToTileRect } from './districtZonesData'

describe('inclusiveBoundsToTileRect', () => {
  it('adds +1 on each axis for inclusive [x1,y1,x2,y2] bounds', () => {
    // Experiment Building: map_data inclusive bounds (108,72)-(124,86).
    expect(inclusiveBoundsToTileRect([108, 72, 124, 86])).toEqual({
      x: 108, y: 72, w: 17, h: 15,
    })
  })

  it('treats a single-tile bound as 1×1, not 0×0', () => {
    expect(inclusiveBoundsToTileRect([5, 5, 5, 5])).toEqual({ x: 5, y: 5, w: 1, h: 1 })
  })

  it('handles a wide/tall rect', () => {
    expect(inclusiveBoundsToTileRect([10, 20, 19, 29])).toEqual({ x: 10, y: 20, w: 10, h: 10 })
  })
})

describe('experiment_building minimap footprint', () => {
  it('is 17×15 at (108,72) — no off-by-one shrink', () => {
    const lab = LOCATIONS.find((l) => l.key === 'experiment_building')
    expect(lab).toBeDefined()
    expect(lab!.tileRect).toEqual({ x: 108, y: 72, w: 17, h: 15 })
  })
})
