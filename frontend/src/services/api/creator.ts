import { apiFetch } from './core'

// ─── Creator dashboard API (D4) ──────────────────────────────────

export interface CreatorSeriesPoint {
  date: string // 'YYYY-MM-DD'
  value: number
}

export interface CreatorWeeklyRating {
  week_start: string
  avg_rating: number | null
  count: number
}

export interface CreatorResidentStats {
  id: string
  slug: string
  name: string
  sprite_key: string
  star_rating: number
  conversations_30d: number
  avg_rating_30d: number | null
  earnings_30d: number
  memories_30d: number
  /** Sparse: only days with ≥1 conversation; zero-fill with fillDailySeries. */
  daily_conversations: CreatorSeriesPoint[]
}

export interface CreatorTotals {
  conversations: number
  earnings_sc: number
  memories: number
  avg_rating: number | null
}

export interface CreatorStatsData {
  window_days: number
  since: string
  residents: CreatorResidentStats[]
  daily_conversations: CreatorSeriesPoint[]
  daily_earnings: CreatorSeriesPoint[]
  weekly_ratings: CreatorWeeklyRating[]
  totals: CreatorTotals
}

export function getCreatorStats(): Promise<CreatorStatsData> {
  return apiFetch('/creator/stats')
}
