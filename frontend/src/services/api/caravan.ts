import { apiFetch } from './core'

export type CaravanPhase =
  | 'scheduled'
  | 'waiting'
  | 'inbound'
  | 'trading'
  | 'outbound'
  | 'departed'
  | 'cancelled'

export interface CaravanTile {
  tile_x: number
  tile_y: number
}

export interface CaravanMotion {
  path: [number, number][]
  started_at: string
  ends_at: string
}

export interface CaravanSummary {
  fee_sc: number
  bought: number
  spent_sc: number
  tax_sc: number
  imports_stocked: number
}

/**
 * Full, server-authoritative caravan projection. GET /caravans/current and the
 * caravan_state WebSocket frame intentionally share this exact shape.
 */
export interface CaravanState {
  type: 'caravan_state'
  visit_id: string | null
  world_event_id: string | null
  version: number
  phase: CaravanPhase | null
  server_time: string
  position: CaravanTile | null
  motion: CaravanMotion | null
  summary: CaravanSummary
  visible: boolean
}

export function getCurrentCaravan(): Promise<CaravanState> {
  return apiFetch('/caravans/current')
}
