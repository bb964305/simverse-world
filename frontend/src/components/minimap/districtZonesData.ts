// Static minimap location data. Kept separate from the DistrictZones
// component so that file only exports components
// (react-refresh/only-export-components).

export { MAP_TILES_H, MAP_TILES_W } from '../../game/worldGeometry'

export type LocationKey =
  | 'academy' | 'tavern' | 'cafe' | 'workshop'
  | 'library' | 'shop' | 'town_hall' | 'experiment_building' | 'market_hall'
  | 'north_path' | 'central_plaza' | 'south_lawn' | 'town_entrance'
  | 'east_gardens' | 'south_quarter'

export interface LocationConfig {
  key: LocationKey
  label: string
  labelEn: string
  icon: string
  color: string
  bgColor: string
  bgColorDim: string
  tileRect: TileRect
}

export interface TileRect { x: number; y: number; w: number; h: number }

// map_data stores location bounds as INCLUSIVE tile ranges [x1, y1, x2, y2]:
// both endpoints are occupied tiles, so the extent is (x2 - x1 + 1) wide, not
// (x2 - x1). Static (this file) and dynamic (DistrictZones overlay) locations
// must convert bounds → rect the same way (test-spec V22), so both go through
// this single helper. Off-by-one here shrinks every rendered footprint by one
// tile on each axis — the defect this fixes for the Experiment Building.
export function inclusiveBoundsToTileRect(bounds: readonly number[]): TileRect {
  const [x1, y1, x2, y2] = bounds
  return { x: x1, y: y1, w: x2 - x1 + 1, h: y2 - y1 + 1 }
}

export const LOCATIONS: LocationConfig[] = [
  // Public facilities — colored
  {
    key: 'academy', label: '学院', labelEn: 'Academy', icon: '🏫',
    color: 'rgba(34,197,94,0.8)', bgColor: 'rgba(34,197,94,0.35)', bgColorDim: 'rgba(34,197,94,0.12)',
    tileRect: inclusiveBoundsToTileRect([15, 18, 42, 34]),
  },
  {
    key: 'tavern', label: '酒馆', labelEn: 'Tavern', icon: '🍺',
    color: 'rgba(239,68,68,0.8)', bgColor: 'rgba(239,68,68,0.35)', bgColorDim: 'rgba(239,68,68,0.12)',
    tileRect: inclusiveBoundsToTileRect([72, 13, 83, 26]),
  },
  {
    key: 'cafe', label: '咖啡馆', labelEn: 'Café', icon: '☕',
    color: 'rgba(245,158,11,0.8)', bgColor: 'rgba(245,158,11,0.35)', bgColorDim: 'rgba(245,158,11,0.12)',
    tileRect: inclusiveBoundsToTileRect([53, 14, 62, 26]),
  },
  {
    key: 'workshop', label: '工坊', labelEn: 'Workshop', icon: '🔨',
    color: 'rgba(168,85,247,0.8)', bgColor: 'rgba(168,85,247,0.35)', bgColorDim: 'rgba(168,85,247,0.12)',
    tileRect: inclusiveBoundsToTileRect([108, 20, 124, 34]),
  },
  {
    key: 'library', label: '图书馆', labelEn: 'Library', icon: '📚',
    color: 'rgba(59,130,246,0.8)', bgColor: 'rgba(59,130,246,0.35)', bgColorDim: 'rgba(59,130,246,0.12)',
    tileRect: inclusiveBoundsToTileRect([57, 43, 70, 53]),
  },
  {
    key: 'shop', label: '杂货铺', labelEn: 'General Store', icon: '🏪',
    color: 'rgba(236,72,153,0.8)', bgColor: 'rgba(236,72,153,0.35)', bgColorDim: 'rgba(236,72,153,0.12)',
    tileRect: inclusiveBoundsToTileRect([75, 43, 93, 53]),
  },
  {
    key: 'town_hall', label: '市政厅', labelEn: 'Town Hall', icon: '🏛',
    color: 'rgba(14,165,233,0.8)', bgColor: 'rgba(14,165,233,0.35)', bgColorDim: 'rgba(14,165,233,0.12)',
    tileRect: inclusiveBoundsToTileRect([106, 45, 132, 62]),
  },
  {
    // Lab / experiment building — bounds mirror map_data inclusive (108,72,124,86)
    // → 17×15 tiles. Derived through the shared converter so it can never drift
    // back to the off-by-one 16×14 footprint again (V17/V22).
    key: 'experiment_building', label: '实验楼', labelEn: 'Experiment Lab', icon: '🧪',
    color: 'rgba(20,184,166,0.8)', bgColor: 'rgba(20,184,166,0.35)', bgColorDim: 'rgba(20,184,166,0.12)',
    tileRect: inclusiveBoundsToTileRect([108, 72, 124, 86]),
  },
  {
    key: 'market_hall', label: '集市大厅', labelEn: 'Market Hall', icon: '🏬',
    color: 'rgba(217,119,6,0.8)', bgColor: 'rgba(217,119,6,0.35)', bgColorDim: 'rgba(217,119,6,0.12)',
    tileRect: inclusiveBoundsToTileRect([105, 89, 119, 99]),
  },
  // Outdoor areas — neutral
  {
    key: 'north_path', label: '北林荫道', labelEn: 'North Promenade', icon: '🌳',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: inclusiveBoundsToTileRect([15, 35, 135, 42]),
  },
  {
    key: 'central_plaza', label: '中央广场', labelEn: 'Central Plaza', icon: '🏠',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: inclusiveBoundsToTileRect([55, 54, 95, 58]),
  },
  {
    key: 'south_lawn', label: '南草坪', labelEn: 'South Lawn', icon: '🌿',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: inclusiveBoundsToTileRect([15, 76, 99, 83]),
  },
  {
    key: 'town_entrance', label: '小镇入口', labelEn: 'Town Entrance', icon: '🚪',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: inclusiveBoundsToTileRect([100, 119, 104, 122]),
  },
  {
    key: 'east_gardens', label: '东岸花园', labelEn: 'Eastbank Gardens', icon: '🌳',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: inclusiveBoundsToTileRect([140, 35, 179, 58]),
  },
  {
    key: 'south_quarter', label: '南苑新区', labelEn: 'South Quarter', icon: '🌿',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: inclusiveBoundsToTileRect([42, 100, 135, 109]),
  },
]

// Backwards-compatible alias for existing imports
export type DistrictKey = LocationKey
export const DISTRICTS = LOCATIONS
