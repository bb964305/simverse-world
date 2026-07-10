import { useGameStore } from '../stores/gameStore'

export const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'

// Requests that hang longer than this are aborted so the UI never spins forever.
const DEFAULT_TIMEOUT_MS = 15000

function getToken(): string | null {
  return localStorage.getItem('token')
}

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token
    ? { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
    : { 'Content-Type': 'application/json' }
}

/**
 * Merge the caller's abort signal (e.g. from a component unmount) with a 15s
 * timeout so both a manual cancel and a stalled request tear the fetch down.
 */
function withTimeout(signal: AbortSignal | null | undefined): AbortSignal {
  const timeout = AbortSignal.timeout(DEFAULT_TIMEOUT_MS)
  return signal ? AbortSignal.any([signal, timeout]) : timeout
}

export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const { signal: callerSignal, ...rest } = options
  let resp: Response
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      ...rest,
      signal: withTimeout(callerSignal),
      headers: { ...authHeaders(), ...(options.headers as Record<string, string> || {}) },
    })
  } catch (err) {
    // Timeout fires as a TimeoutError; a caller unmount fires as AbortError.
    if (err instanceof DOMException && err.name === 'TimeoutError') {
      throw new Error(`请求超时（${DEFAULT_TIMEOUT_MS / 1000}s）：${path}`)
    }
    throw err
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      // Centralize logout through the store: it clears token + user (state and
      // localStorage) once. ProtectedRoute then redirects to /login, avoiding
      // the multiple hard `window.location` jumps concurrent 401s used to cause.
      useGameStore.getState().logout()
      throw new Error('Session expired')
    }
    const body = await resp.text()
    throw new Error(`API ${resp.status}: ${body}`)
  }
  return resp.json() as Promise<T>
}

export interface ForgeStartResponse {
  forge_id: string
  step: number
  question: string
}

export interface ForgeAnswerResponse {
  forge_id: string
  step: number
  next_step: number | null
  question: string | null
  ability_md: string | null
  persona_md: string | null
  soul_md: string | null
}

export interface ForgeStatusResponse {
  forge_id: string
  status: 'collecting' | 'generating' | 'done' | 'error'
  step: number
  name: string
  answers: Record<string, string>
  ability_md: string
  persona_md: string
  soul_md: string
  star_rating: number
  district: string
  resident_id: string | null
  error: string | null
}

export function forgeStart(name: string): Promise<ForgeStartResponse> {
  return apiFetch('/forge/start', { method: 'POST', body: JSON.stringify({ name }) })
}

export function forgeAnswer(forge_id: string, answer: string): Promise<ForgeAnswerResponse> {
  return apiFetch('/forge/answer', { method: 'POST', body: JSON.stringify({ forge_id, answer }) })
}

export function forgeStatus(forge_id: string): Promise<ForgeStatusResponse> {
  return apiFetch(`/forge/status/${forge_id}`)
}

export function forgeQuick(name: string, raw_text: string): Promise<{ forge_id: string; status: string }> {
  return apiFetch('/forge/quick', { method: 'POST', body: JSON.stringify({ name, raw_text }) })
}

export interface DeepForgeStartResponse {
  forge_id: string
  status: string
}

export type DeepForgeStage =
  | 'routing'
  | 'researching'
  | 'extracting'
  | 'building'
  | 'validating'
  | 'refining'
  | 'done'
  | 'error'

export interface DeepForgeStatusResponse {
  forge_id: string
  status: DeepForgeStage
  stage: DeepForgeStage
  progress: number
  name: string
  ability_md: string | null
  persona_md: string | null
  soul_md: string | null
  star_rating: number
  district: string
  resident_id: string | null
  error: string | null
}

export function deepForgeStart(
  token: string,
  data: { character_name: string; raw_text?: string; user_material?: string },
): Promise<DeepForgeStartResponse> {
  return apiFetch('/forge/deep-start', {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function deepForgeStatus(token: string, forgeId: string): Promise<DeepForgeStatusResponse> {
  return apiFetch(`/forge/deep-status/${forgeId}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export interface ImportSkillResponse {
  id: string
  slug: string
  name: string
  district: string
  star_rating: number
  ability_md: string
  persona_md: string
  soul_md: string
  meta_json: Record<string, unknown> | null
}

// ─── Onboarding API ──────────────────────────────────────────────

export interface OnboardingCheckResponse {
  needs_onboarding: boolean
  player_resident_id: string | null
}

export interface ResidentListItem {
  id: string
  slug: string
  name: string
  district: string
  status: string
  heat: number
  sprite_key: string
  tile_x: number
  tile_y: number
  star_rating: number
  token_cost_per_turn: number
  meta_json: Record<string, unknown> | null
  mood_label?: string
}

export interface OnboardingResidentResponse {
  id: string
  slug: string
  name: string
  sprite_key: string
  tile_x: number
  tile_y: number
}

export function checkOnboarding(token: string): Promise<OnboardingCheckResponse> {
  return apiFetch('/onboarding/check', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getResidents(): Promise<ResidentListItem[]> {
  return apiFetch('/residents')
}

export function loadPreset(token: string, preset_slug: string): Promise<OnboardingResidentResponse> {
  return apiFetch('/onboarding/load-preset', {
    method: 'POST',
    body: JSON.stringify({ preset_slug }),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function skipOnboarding(token: string): Promise<OnboardingResidentResponse> {
  return apiFetch('/onboarding/skip', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Settings API ────────────────────────────────────────────────

export interface AccountSettings {
  display_name: string
  email: string
  has_password: boolean
  github_bound: boolean
  linuxdo_bound: boolean
  linuxdo_trust_level: number | null
}

export interface CharacterSettings {
  resident_id: string
  name: string
  sprite_key: string
  portrait_url: string | null
  ability_md: string
  persona_md: string
  soul_md: string
}

export interface SpriteTemplate {
  key: string
  gender: string
  age_group: string
  vibe: string
  tags: string[]
}

export interface AllSettings {
  account: AccountSettings
  character: CharacterSettings | null
  interaction: Record<string, unknown>
  privacy: Record<string, unknown>
  llm: Record<string, unknown>
  economy: Record<string, unknown>
}

export function getSettings(): Promise<AllSettings> {
  return apiFetch('/settings')
}

export function updateAccount(data: { display_name?: string }): Promise<{ display_name: string; email: string }> {
  return apiFetch('/settings/account', { method: 'PATCH', body: JSON.stringify(data) })
}

export function updateCharacter(data: { name?: string; sprite_key?: string }): Promise<CharacterSettings> {
  return apiFetch('/settings/character', { method: 'PATCH', body: JSON.stringify(data) })
}

export function updateInteraction(data: {
  reply_mode?: 'manual' | 'auto'
  offline_auto_reply?: boolean
  notification_chat?: boolean
  notification_system?: boolean
}): Promise<{ interaction: Record<string, unknown> }> {
  return apiFetch('/settings/interaction', { method: 'PATCH', body: JSON.stringify(data) })
}

export function updatePrivacy(data: {
  map_visible?: boolean
  persona_visibility?: 'full' | 'identity_card_only' | 'hidden'
  allow_conversation_stats?: boolean
}): Promise<{ privacy: Record<string, unknown> }> {
  return apiFetch('/settings/privacy', { method: 'PATCH', body: JSON.stringify(data) })
}

export function getSpriteTemplates(): Promise<SpriteTemplate[]> {
  return apiFetch('/sprites/templates')
}

export interface LLMSettings {
  system_allows_custom?: boolean
  custom_llm_enabled?: boolean
  api_format?: 'openai' | 'anthropic'
  base_url?: string
  api_key?: string
  model?: string
}

export interface LLMTestResult {
  success: boolean
  message: string
}

export function updateLLM(data: LLMSettings): Promise<{ llm: Record<string, unknown> }> {
  return apiFetch('/settings/llm', { method: 'PATCH', body: JSON.stringify(data) })
}

export function testLLMConnection(data: {
  api_format: string
  base_url: string
  api_key: string
  model: string
}): Promise<LLMTestResult> {
  return apiFetch('/settings/llm/test', { method: 'POST', body: JSON.stringify(data) })
}

export function updateEconomy(data: { low_balance_alert?: number }): Promise<{ economy: Record<string, unknown> }> {
  return apiFetch('/settings/economy', { method: 'PATCH', body: JSON.stringify(data) })
}

// ─── Admin API ───────────────────────────────────────────────────

export interface AdminDashboardStats {
  online_users: number
  today_registrations: number
  active_chats: number
  soul_coin_net_flow: number
}

export interface AdminDashboardHealth {
  searxng: 'ok' | 'error'
  llm_api: 'ok' | 'error'
  details: Record<string, string | null>
}

export interface AdminDashboardTrend {
  date: string
  registrations: number
  active_users: number
  sc_spent: number
}

export function getAdminDashboardStats(token: string): Promise<AdminDashboardStats> {
  return apiFetch('/admin/dashboard/stats', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export async function getAdminDashboardHealth(token: string): Promise<AdminDashboardHealth> {
  const arr: { service: string; status: string; detail?: string | null }[] = await apiFetch('/admin/dashboard/health', {
    headers: { Authorization: `Bearer ${token}` },
  })
  // Transform array to keyed object
  const result: AdminDashboardHealth = { searxng: 'error', llm_api: 'error', details: {} }
  for (const item of arr) {
    const key = item.service as keyof Omit<AdminDashboardHealth, 'details'>
    if (key in result) {
      result[key] = item.status as 'ok' | 'error'
    }
    result.details[item.service] = item.detail ?? null
  }
  return result
}

export function getAdminDashboardTrends(token: string): Promise<AdminDashboardTrend[]> {
  return apiFetch('/admin/dashboard/trends', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Admin Economy API ───────────────────────────────────────────

export interface AdminEconomyStats {
  total_issued: number
  total_consumed: number
  net_circulation: number
  total_users: number
  avg_balance: number
}

export interface AdminTransaction {
  id: string
  user_id: string
  amount: number
  reason: string
  created_at: string
}

export interface AdminTransactionsResponse {
  items: AdminTransaction[]
  total: number
  offset: number
  limit: number
}

export interface AdminEconomyConfig {
  signup_bonus: number
  daily_reward: number
  chat_cost_per_turn: number
  creator_reward: number
  rating_bonus: number
}

export function getAdminEconomyStats(token: string): Promise<AdminEconomyStats> {
  return apiFetch('/admin/economy/stats', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminTransactions(
  token: string,
  params: { page?: number; per_page?: number; reason?: string },
): Promise<AdminTransactionsResponse> {
  const qs = new URLSearchParams()
  const offset = ((params.page ?? 1) - 1) * (params.per_page ?? 20)
  qs.set('offset', String(offset))
  qs.set('limit', String(params.per_page ?? 20))
  if (params.reason) qs.set('reason', params.reason)
  const query = qs.toString() ? `?${qs.toString()}` : ''
  return apiFetch(`/admin/economy/transactions${query}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminEconomyConfig(token: string): Promise<AdminEconomyConfig> {
  return apiFetch('/admin/economy/config', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function updateAdminEconomyConfig(token: string, data: Partial<AdminEconomyConfig>): Promise<AdminEconomyConfig> {
  return apiFetch('/admin/economy/config', {
    method: 'PUT',
    body: JSON.stringify(data),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminSystemConfig(token: string, group: string): Promise<Record<string, unknown>> {
  return apiFetch(`/admin/system/groups/${group}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function updateAdminSystemConfig(
  token: string,
  key: string,
  value: unknown,
  group: string,
): Promise<{ key: string; value: unknown; group: string }> {
  return apiFetch('/admin/system/entry', {
    method: 'PUT',
    body: JSON.stringify({ key, value, group }),
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Admin Residents & Forge Monitor API ─────────────────────────

export interface AdminResident {
  id: string
  slug: string
  name: string
  district: string
  status: string
  heat: number
  star_rating: number
  sprite_key: string
  type: 'NPC' | 'Player'
  creator: string | null
  ability_md: string | null
  persona_md: string | null
  soul_md: string | null
  meta_json: Record<string, unknown> | null
}

export interface AdminResidentsResponse {
  items: AdminResident[]
  total: number
  page: number
  per_page: number
}

export function getAdminResidents(
  token: string,
  params: { page?: number; per_page?: number; search?: string; district?: string; status?: string },
): Promise<AdminResidentsResponse> {
  const qs = new URLSearchParams()
  if (params.page != null) qs.set('page', String(params.page))
  if (params.per_page != null) qs.set('per_page', String(params.per_page))
  if (params.search) qs.set('search', params.search)
  if (params.district) qs.set('district', params.district)
  if (params.status) qs.set('status', params.status)
  const query = qs.toString() ? `?${qs.toString()}` : ''
  return apiFetch(`/admin/residents${query}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function adminEditResident(
  token: string,
  residentId: string,
  data: Record<string, unknown>,
): Promise<AdminResident> {
  return apiFetch(`/admin/residents/${residentId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export interface AdminForgeSession {
  forge_id: string
  character_name: string
  mode: 'quick' | 'deep'
  current_stage: string
  status: string
  started_at: string
  elapsed_seconds: number
}

export interface AdminForgeHistoryItem {
  forge_id: string
  character_name: string
  mode: 'quick' | 'deep'
  status: string
  stage: string
  started_at: string
  finished_at: string | null
  resident_id: string | null
}

export interface AdminForgeHistoryResponse {
  items: AdminForgeHistoryItem[]
  total: number
  page: number
  per_page: number
}

export function getAdminForgeActive(token: string): Promise<AdminForgeSession[]> {
  return apiFetch('/admin/forge/active', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminForgeHistory(
  token: string,
  params: { page?: number; per_page?: number; status?: string },
): Promise<AdminForgeHistoryResponse> {
  const qs = new URLSearchParams()
  if (params.page != null) qs.set('page', String(params.page))
  if (params.per_page != null) qs.set('per_page', String(params.per_page))
  if (params.status) qs.set('status', params.status)
  const query = qs.toString() ? `?${qs.toString()}` : ''
  return apiFetch(`/admin/forge${query}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Import Skill ────────────────────────────────────────────────

export async function importSkill(file: File, name: string, slug: string): Promise<ImportSkillResponse> {
  const token = getToken()
  const formData = new FormData()
  formData.append('file', file)
  formData.append('name', name)
  formData.append('slug', slug)

  const resp = await fetch(`${API_BASE}/residents/import`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  })
  if (!resp.ok) {
    const body = await resp.text()
    throw new Error(`API ${resp.status}: ${body}`)
  }
  return resp.json() as Promise<ImportSkillResponse>
}

// ─── Admin Users API ─────────────────────────────────────────────

export interface AdminUserListParams {
  page?: number
  per_page?: number
  search?: string
  sort_by?: string
}

export interface AdminUserListItem {
  id: string
  name: string
  email: string
  login_methods: string[]
  soul_coin_balance: number
  resident_count: number
  created_at: string
  is_banned: boolean
  is_admin: boolean
}

export interface AdminUserListResponse {
  items: AdminUserListItem[]
  total: number
  page: number
  per_page: number
}

export interface AdminUserDetail extends AdminUserListItem {
  last_login_at: string | null
}

export interface AdminAdjustCoinData {
  amount: number
  reason: string
}

export interface AdminPatchUserData {
  is_banned?: boolean
  is_admin?: boolean
}

export function getAdminUsers(token: string, params: AdminUserListParams): Promise<AdminUserListResponse> {
  const qs = new URLSearchParams()
  if (params.page !== undefined) qs.set('page', String(params.page))
  if (params.per_page !== undefined) qs.set('per_page', String(params.per_page))
  if (params.search) qs.set('search', params.search)
  if (params.sort_by) qs.set('sort_by', params.sort_by)
  const query = qs.toString() ? `?${qs.toString()}` : ''
  return apiFetch(`/admin/users${query}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminUserDetail(token: string, userId: string): Promise<AdminUserDetail> {
  return apiFetch(`/admin/users/${userId}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function adminAdjustCoin(
  token: string,
  userId: string,
  data: AdminAdjustCoinData,
): Promise<{ new_balance: number }> {
  return apiFetch(`/admin/users/${userId}/adjust-coin`, {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function adminPatchUser(
  token: string,
  userId: string,
  data: AdminPatchUserData,
): Promise<{ user_id: string; is_banned: boolean; is_admin: boolean }> {
  return apiFetch(`/admin/users/${userId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Player Position API ─────────────────────────────────────────

export function updatePlayerPosition(tileX: number, tileY: number): Promise<{ tile_x: number; tile_y: number }> {
  return apiFetch('/residents/player/position', {
    method: 'PUT',
    body: JSON.stringify({ tile_x: tileX, tile_y: tileY }),
  })
}

// ── Notifications (S4) ──────────────────────────────────────────────
import type { NotificationItem } from '../stores/gameStore'

export interface NotificationListResponse {
  notifications: NotificationItem[]
  unread_count: number
  next_cursor: string | null
}

export function getNotifications(opts: { unreadOnly?: boolean; cursor?: string } = {}): Promise<NotificationListResponse> {
  const params = new URLSearchParams()
  if (opts.unreadOnly) params.set('unread_only', 'true')
  if (opts.cursor) params.set('cursor', opts.cursor)
  const qs = params.toString()
  return apiFetch(`/notifications${qs ? `?${qs}` : ''}`)
}

export function markNotificationsRead(ids: string[]): Promise<{ unread_count: number }> {
  return apiFetch('/notifications/read', {
    method: 'POST',
    body: JSON.stringify({ ids }),
  })
}

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

// ── Resident life goals (A1) ────────────────────────────────────────
export interface ResidentGoalData {
  id: string
  kind: string
  title: string
  motivation: string
  status: string
  progress: number
  milestones: Array<{ title: string; done: boolean; at?: string }>
  resolved_at: string | null
}

export function getResidentGoals(slug: string): Promise<{ active: ResidentGoalData | null; resolved: ResidentGoalData[] }> {
  return apiFetch(`/residents/${encodeURIComponent(slug)}/goals`)
}

// ── Relationship graph (C2) ─────────────────────────────────────────
export interface GraphNode { slug: string; name: string; portrait_url: string | null; district: string }
export interface GraphEdge { a: string; b: string; strength: number; label: string; mutual: boolean }

export function getRelationshipGraph(minImportance = 0.3): Promise<{ nodes: GraphNode[]; edges: GraphEdge[] }> {
  return apiFetch(`/graph/relationships?min_importance=${minImportance}`)
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

// ── Follow feed (E11) ───────────────────────────────────────────────
export interface FeedEventData {
  id: string
  resident_slug: string
  kind: string
  payload: Record<string, unknown>
  created_at: string | null
}

export interface FeedResponse {
  events: FeedEventData[]
  next_cursor: string | null
}

export function getFeed(cursor?: string): Promise<FeedResponse> {
  return apiFetch(`/feed${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`)
}

export function followResident(slug: string): Promise<{ ok: boolean }> {
  return apiFetch(`/follows/${encodeURIComponent(slug)}`, { method: 'POST' })
}

export function unfollowResident(slug: string): Promise<{ ok: boolean }> {
  return apiFetch(`/follows/${encodeURIComponent(slug)}`, { method: 'DELETE' })
}

// ── Bulletin posts (A4) ─────────────────────────────────────────────
export interface BulletinPostData {
  id: string
  kind: string
  title: string
  content_md: string
  likes: number
  tips_sc: number
  pinned: boolean
  author_resident_id: string | null
  author_name: string | null
  author_portrait: string | null
  created_at: string | null
}

export interface BulletinPostsResponse {
  posts: BulletinPostData[]
  next_cursor: string | null
}

export function getBulletinPosts(opts: { kind?: string; cursor?: string } = {}): Promise<BulletinPostsResponse> {
  const params = new URLSearchParams()
  if (opts.kind) params.set('kind', opts.kind)
  if (opts.cursor) params.set('cursor', opts.cursor)
  const qs = params.toString()
  return apiFetch(`/bulletin/posts${qs ? `?${qs}` : ''}`)
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

// ── TTS (E5) ────────────────────────────────────────────────────────
export interface TTSResponse {
  url: string
  duration: number
  cached: boolean
  voice: string
}

// 429 = daily quota exhausted (surface as inline notice, not a hard error).
export function ttsSpeak(resident_slug: string, text: string): Promise<TTSResponse> {
  return apiFetch('/tts', {
    method: 'POST',
    body: JSON.stringify({ resident_slug, text }),
  })
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

// ─── Current user ────────────────────────────────────────────────

export interface MeResponse {
  id: string
  name: string
  email: string
  avatar: string | null
  soul_coin_balance: number
  is_admin: boolean
}

export function getMe(): Promise<MeResponse> {
  return apiFetch('/users/me')
}

// ─── Admin LLM Usage API ─────────────────────────────────────────

export interface LlmUsageScenario {
  calls: number
  input_tokens: number
  output_tokens: number
  est_cost_usd: number
}

export interface LlmUsageSummary {
  hours: number
  scenarios: Record<string, LlmUsageScenario>
  total: LlmUsageScenario
}

export function getAdminLlmUsageSummary(token: string, hours: number): Promise<LlmUsageSummary> {
  return apiFetch(`/admin/llm-usage/summary?hours=${hours}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}
