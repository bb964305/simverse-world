import { apiFetch } from './core'

// ─── Home decor (B3) ─────────────────────────────────────────────

export interface DecorItem {
  item_code: string
  /** Tile offset from the home bbox top-left corner. */
  x: number
  y: number
  rot: number
}

export interface HomeDecorResponse {
  resident_slug: string
  home_location_id: string | null
  bounds: [number, number, number, number] | null
  items: DecorItem[]
}

export function getHomeDecor(slug: string): Promise<HomeDecorResponse> {
  return apiFetch(`/residents/${encodeURIComponent(slug)}/home/decor`)
}

/** Full replace; owner only (403 otherwise, 400 on validation). */
export function putHomeDecor(slug: string, items: DecorItem[]): Promise<HomeDecorResponse> {
  return apiFetch(`/residents/${encodeURIComponent(slug)}/home/decor`, {
    method: 'PUT',
    body: JSON.stringify({ items }),
  })
}

export const DECOR_MAX_ITEMS = 12

// No decor sprite atlas exists yet — items render as emoji mapped from
// item_code (recorded deviation from the spec's public/assets/decor/ atlas).
export const DECOR_EMOJI: Record<string, string> = {
  decor_lamp: '🪔',
  decor_plant: '🪴',
  decor_rug: '🟫',
}

export function decorEmoji(itemCode: string): string {
  return DECOR_EMOJI[itemCode] ?? '📦'
}

// Housing bboxes (tile coords, inclusive) mirrored from
// backend app/agent/map_data.py — static map data, same precedent as
// components/minimap/districtZonesData.ts.
export const HOUSING_BOUNDS: Record<string, [number, number, number, number]> = {
  house_a: [65, 14, 69, 26],
  house_b: [86, 13, 90, 25],
  house_c: [93, 13, 97, 25],
  house_d: [20, 59, 24, 70],
  house_e: [27, 59, 33, 70],
  house_f: [36, 59, 40, 70],
  apt_star: [51, 65, 62, 75],
  apt_moon: [69, 65, 80, 75],
  apt_dawn: [87, 65, 99, 75],
}

export const HOUSING_NAMES: Record<string, string> = {
  house_a: '住宅A', house_b: '住宅B', house_c: '住宅C',
  house_d: '住宅D', house_e: '住宅E', house_f: '住宅F',
  apt_star: '星光公寓', apt_moon: '月华公寓', apt_dawn: '晨曦公寓',
}
