// Static minimap location data. Kept separate from the DistrictZones
// component so that file only exports components
// (react-refresh/only-export-components).

export { MAP_TILES_H, MAP_TILES_W } from '../../game/worldGeometry'

export type LocationKey =
  | 'academy' | 'tavern' | 'cafe' | 'workshop'
  | 'library' | 'shop' | 'town_hall' | 'experiment_building'
  | 'north_path' | 'central_plaza' | 'south_lawn' | 'town_entrance'
  | 'east_gardens' | 'south_quarter'

export interface LocationConfig {
  key: LocationKey
  label: string
  icon: string
  color: string
  bgColor: string
  bgColorDim: string
  tileRect: { x: number; y: number; w: number; h: number }
}

export const LOCATIONS: LocationConfig[] = [
  // Public facilities — colored
  {
    key: 'academy', label: '学院', icon: '🏫',
    color: 'rgba(34,197,94,0.8)', bgColor: 'rgba(34,197,94,0.35)', bgColorDim: 'rgba(34,197,94,0.12)',
    tileRect: { x: 25, y: 18, w: 17, h: 16 },
  },
  {
    key: 'tavern', label: '酒馆', icon: '🍺',
    color: 'rgba(239,68,68,0.8)', bgColor: 'rgba(239,68,68,0.35)', bgColorDim: 'rgba(239,68,68,0.12)',
    tileRect: { x: 72, y: 13, w: 11, h: 13 },
  },
  {
    key: 'cafe', label: '咖啡馆', icon: '☕',
    color: 'rgba(245,158,11,0.8)', bgColor: 'rgba(245,158,11,0.35)', bgColorDim: 'rgba(245,158,11,0.12)',
    tileRect: { x: 53, y: 14, w: 9, h: 12 },
  },
  {
    key: 'workshop', label: '工坊', icon: '🔨',
    color: 'rgba(168,85,247,0.8)', bgColor: 'rgba(168,85,247,0.35)', bgColorDim: 'rgba(168,85,247,0.12)',
    tileRect: { x: 108, y: 20, w: 16, h: 14 },
  },
  {
    key: 'library', label: '图书馆', icon: '📚',
    color: 'rgba(59,130,246,0.8)', bgColor: 'rgba(59,130,246,0.35)', bgColorDim: 'rgba(59,130,246,0.12)',
    tileRect: { x: 57, y: 43, w: 13, h: 10 },
  },
  {
    key: 'shop', label: '杂货铺', icon: '🏪',
    color: 'rgba(236,72,153,0.8)', bgColor: 'rgba(236,72,153,0.35)', bgColorDim: 'rgba(236,72,153,0.12)',
    tileRect: { x: 75, y: 43, w: 18, h: 10 },
  },
  {
    key: 'town_hall', label: '市政厅', icon: '🏛',
    color: 'rgba(14,165,233,0.8)', bgColor: 'rgba(14,165,233,0.35)', bgColorDim: 'rgba(14,165,233,0.12)',
    tileRect: { x: 106, y: 45, w: 26, h: 17 },
  },
  {
    // Lab / experiment building — bounds mirror map_data (108,72,124,86).
    key: 'experiment_building', label: '实验楼', icon: '🧪',
    color: 'rgba(20,184,166,0.8)', bgColor: 'rgba(20,184,166,0.35)', bgColorDim: 'rgba(20,184,166,0.12)',
    tileRect: { x: 108, y: 72, w: 16, h: 14 },
  },
  // Outdoor areas — neutral
  {
    key: 'north_path', label: '北林荫道', icon: '🌳',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: { x: 15, y: 35, w: 120, h: 7 },
  },
  {
    key: 'central_plaza', label: '中央广场', icon: '🏠',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: { x: 55, y: 54, w: 40, h: 4 },
  },
  {
    key: 'south_lawn', label: '南草坪', icon: '🌿',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: { x: 15, y: 76, w: 84, h: 7 },
  },
  {
    key: 'town_entrance', label: '小镇入口', icon: '🚪',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: { x: 50, y: 85, w: 40, h: 14 },
  },
  {
    key: 'east_gardens', label: '东岸花园', icon: '🌳',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: { x: 140, y: 35, w: 39, h: 23 },
  },
  {
    key: 'south_quarter', label: '南苑新区', icon: '🌿',
    color: 'rgba(100,116,139,0.6)', bgColor: 'rgba(100,116,139,0.2)', bgColorDim: 'rgba(100,116,139,0.08)',
    tileRect: { x: 42, y: 100, w: 93, h: 9 },
  },
]

// Backwards-compatible alias for existing imports
export type DistrictKey = LocationKey
export const DISTRICTS = LOCATIONS
