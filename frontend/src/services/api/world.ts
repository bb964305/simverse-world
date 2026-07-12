import { apiFetch } from './core'

// ── Achievements (S2/D1) ────────────────────────────────────────────
export interface AchievementEntry {
  code: string
  title: string
  description: string
  icon: string
  points: number
  reward_sc: number
  hidden: boolean
  unlocked: boolean
  unlocked_at: string | null
  progress: { count?: number; target?: number } | null
}

export function getAchievements(): Promise<{ achievements: AchievementEntry[] }> {
  return apiFetch('/achievements')
}

// ── Digest (A5) ─────────────────────────────────────────────────────
export interface DigestData {
  id: string
  scope: string
  date: string
  title: string
  content_md: string
  stats: Record<string, unknown>
  created_at: string | null
}

export function getLatestDigest(): Promise<{ digest: DigestData | null }> {
  return apiFetch('/digest/latest')
}

// ── Commissions (B1) ────────────────────────────────────────────────
export interface CommissionData {
  id: string
  issuer_resident_id: string
  kind: string
  title: string
  payload: Record<string, unknown>
  reward_sc: number
  status: string
  acceptor_user_id: string | null
  expires_at: string | null
}

export function getCommissions(status = 'open'): Promise<{ commissions: CommissionData[] }> {
  return apiFetch(`/commissions?status=${encodeURIComponent(status)}`)
}

export function acceptCommission(id: string): Promise<CommissionData> {
  return apiFetch(`/commissions/${id}/accept`, { method: 'POST' })
}

export function abandonCommission(id: string): Promise<{ ok: boolean }> {
  return apiFetch(`/commissions/${id}/abandon`, { method: 'POST' })
}

// ── Seasons & Polls (E12/C3) ────────────────────────────────────────
export interface SeasonInfo {
  id: string
  title: string
  theme: string
  status: string
  ends_at: string | null
}

export interface LeaderboardRow {
  rank: number
  user_id: string
  name?: string
  points: number
  breakdown: Record<string, number>
}

export interface LeaderboardAroundRow {
  rank: number
  user_id: string
  name?: string
  points: number
}

export interface LeaderboardResponse {
  top: LeaderboardRow[]
  around_me?: { my_rank: number; rows: LeaderboardAroundRow[] }
}

export interface PollData {
  id: string
  season_id: string
  question: string
  options: string[]
  closes_at: string | null
  my_vote?: number
}

export function getCurrentSeason(): Promise<{ season: SeasonInfo | null }> {
  return apiFetch('/seasons/current')
}

export function getSeasonLeaderboard(aroundMe = true): Promise<LeaderboardResponse> {
  return apiFetch(`/seasons/current/leaderboard?around_me=${aroundMe}`)
}

export function getOpenPolls(): Promise<{ polls: PollData[] }> {
  return apiFetch('/polls/open')
}

export function votePoll(pollId: string, optionIdx: number): Promise<{ ok: boolean }> {
  return apiFetch(`/polls/${pollId}/vote`, {
    method: 'POST',
    body: JSON.stringify({ option_idx: optionIdx }),
  })
}

// ── Debates (E9) ────────────────────────────────────────────────────
export type DebateSide = 'a' | 'b'
// Lifecycle (backend debate_service.py): announced → live → voting → settled.
// Stakes only in "announced", free votes only in "voting"; winner a|b|draw.
export type DebateStatus = 'announced' | 'live' | 'voting' | 'settled'

export interface DebateTurn {
  round: number
  side: DebateSide
  speaker: string
  text: string
}

export interface DebateView {
  id: string
  topic: string
  status: DebateStatus
  resident_a_slug: string
  resident_b_slug: string
  pool_a: number
  pool_b: number
  votes_a: number
  votes_b: number
  winner: string | null
  transcript: DebateTurn[]
}

export function getDebates(): Promise<{ debates: DebateView[] }> {
  return apiFetch('/debates')
}

export function getDebate(debateId: string): Promise<DebateView> {
  return apiFetch(`/debates/${debateId}`)
}

export function stakeDebate(
  debateId: string,
  side: DebateSide,
  amount: number,
): Promise<{ id: string; side: DebateSide; amount: number }> {
  return apiFetch(`/debates/${debateId}/stake`, {
    method: 'POST',
    body: JSON.stringify({ side, amount }),
  })
}

export function voteDebate(debateId: string, side: DebateSide): Promise<{ ok: boolean }> {
  return apiFetch(`/debates/${debateId}/vote`, {
    method: 'POST',
    body: JSON.stringify({ side }),
  })
}

// ── Weekly recap (E14) ──────────────────────────────────────────────
export interface WeeklyRecapStats {
  chats: number
  turns: number
  distinct_residents: number
  achievements: number
  explored: number
  tag: string
}

export interface WeeklyRecapData {
  id: string
  scope: string
  date: string
  title: string
  content_md: string
  stats: WeeklyRecapStats
  created_at: string | null
}

export function getWeeklyRecap(): Promise<{ digest: WeeklyRecapData }> {
  return apiFetch('/digest/weekly/me')
}

// ── Daily quest & login streak (D3) ─────────────────────────────────
export interface DailyQuestData {
  id: string
  date: string
  quest: {
    resident_slug: string
    resident_name: string
    topic: string
    min_turns: number
  }
  status: 'pending' | 'done'
  reward_sc: number
}

export interface DailyQuestResponse {
  quest: DailyQuestData | null
  login_streak: number
}

export function getDailyQuest(): Promise<DailyQuestResponse> {
  return apiFetch('/daily/quest')
}

// ── Active world events (A2) ────────────────────────────────────────
export interface ActiveEventData {
  id: string
  type: string
  title: string
  description: string
  payload_json: Record<string, unknown> | null
  starts_at: string | null
  ends_at: string | null
}

export function getActiveEvents(): Promise<{ events: ActiveEventData[] }> {
  return apiFetch('/events/active')
}

// ─── Time capsules (E7) ──────────────────────────────────────────

export interface CapsuleData {
  id: string
  carrier_resident_slug: string
  deliver_on: string
  status: 'sealed' | 'delivered'
  content: string | null
  resident_note: string | null
  delivered_at: string | null
  created_at: string | null
}

export function getCapsules(): Promise<{ capsules: CapsuleData[] }> {
  return apiFetch('/capsules')
}

export function createCapsule(
  carrier_resident_slug: string, deliver_on: string, content: string,
): Promise<CapsuleData> {
  return apiFetch('/capsules', {
    method: 'POST',
    body: JSON.stringify({ carrier_resident_slug, deliver_on, content }),
  })
}

// ─── Group photo (E10) ───────────────────────────────────────────

export function logPhoto(resident_slug: string, media_url?: string): Promise<{ resident_slug: string; quip: string }> {
  return apiFetch('/photos/log', {
    method: 'POST',
    body: JSON.stringify({ resident_slug, media_url: media_url ?? null }),
  })
}
