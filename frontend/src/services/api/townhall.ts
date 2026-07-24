import { apiFetch } from './core'

// Read-only 市政厅 (town hall) client — mirrors backend routers/townhall.py
// (Society Expansion §10). Every field is a projection over existing M1–M6
// data; there are no write endpoints here.

export interface TownHallMayor {
  slug: string
  name: string
}

export interface TownHallDuty {
  key: string
  title: string
  holder_slug: string
  holder_name: string
}

export interface TownHallPoll {
  id: string
  season_id: string | null
  question: string
  options: Array<Record<string, unknown>>
  closes_at: string | null
  my_vote?: number
}

export interface TownHallElection {
  question: string
  closed_at: string | null
  winner_slug: string | null
  winner_name: string | null
  winner_votes: number | null
  options: Array<Record<string, unknown>>
}

export interface TownHallFinances {
  npc_default_wage_sc?: number
  npc_meal_cost_sc?: number
  market_day_weekday?: number
  market_day_discount?: number
  civic_poll_days?: number
  election_mayor_wage_bonus?: number
  election_interval_days?: number
}

export interface TownHallOverview {
  mayor: TownHallMayor | null
  duties: TownHallDuty[]
  open_polls: TownHallPoll[]
  recent_election: TownHallElection | null
  finances: TownHallFinances
}

export interface MarketDayInfo {
  active: boolean
  discount: number
  weekday: number
}

export function getTownHallOverview(): Promise<TownHallOverview> {
  return apiFetch('/townhall/overview')
}

export function getMarketDay(): Promise<MarketDayInfo> {
  return apiFetch('/townhall/market-day')
}
