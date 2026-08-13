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

describe('town_entrance minimap footprint', () => {
  it('tracks the authored south gate instead of the retired wooded belt', () => {
    const gate = LOCATIONS.find((l) => l.key === 'town_entrance')
    expect(gate).toBeDefined()
    expect(gate!.tileRect).toEqual({ x: 100, y: 119, w: 5, h: 4 })
  })
})

describe('market_hall minimap footprint', () => {
  it('renders the standalone 15×11 building at its authored position', () => {
    const market = LOCATIONS.find((l) => l.key === 'market_hall')
    expect(market).toBeDefined()
    expect(market!.tileRect).toEqual({ x: 105, y: 89, w: 15, h: 11 })
  })
})

describe('static location overlay contract', () => {
  it('keeps every minimap footprint aligned with backend inclusive bounds', () => {
    const expected = {
      academy: [15, 18, 42, 34], tavern: [72, 13, 83, 26],
      cafe: [53, 14, 62, 26], workshop: [108, 20, 124, 34],
      library: [57, 43, 70, 53], shop: [75, 43, 93, 53],
      town_hall: [106, 45, 132, 62], experiment_building: [108, 72, 124, 86],
      market_hall: [105, 89, 119, 99],
      north_path: [15, 35, 135, 42], central_plaza: [55, 54, 95, 58],
      south_lawn: [15, 76, 99, 83], town_entrance: [100, 119, 104, 122],
      east_gardens: [140, 35, 179, 58], south_quarter: [42, 100, 135, 109],
    } as const

    for (const [key, bounds] of Object.entries(expected)) {
      expect(LOCATIONS.find((location) => location.key === key)?.tileRect)
        .toEqual(inclusiveBoundsToTileRect(bounds))
    }
  })
})
