import { apiFetch } from './core'

// ─── Admin API ───────────────────────────────────────────────────

export interface AdminDashboardStats {
  online_users: number
  today_registrations: number
  active_chats: number
  soul_coin_net_flow: number
}

export interface AdminDashboardHealth {
  searxng: AdminServiceStatus
  llm_api: AdminServiceStatus
  details: Record<string, string | null>
}

export type AdminServiceStatus = 'ok' | 'error' | 'timeout'

export interface AdminDashboardTrend {
  date: string
  users: number
  conversations: number
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
      result[key] = item.status as AdminServiceStatus
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

export interface AdminEconomyOperations {
  generated_at: string
  gates: Record<string, boolean>
  residents: {
    count: number
    total_balance_sc: number
    median_balance_sc: number
    min_balance_sc: number
    max_balance_sc: number
    poverty_line_sc: number
    poverty_count: number
    poverty_share: number
    eligible_for_cheapest_import: number
    eligible_for_resident_work: number
    world_day_purchase_cap: number
  }
  town: {
    balance_sc: number
    daily_payroll_sc: number
    payroll_runway_world_days: number | null
    target_payroll_days: number
  }
  caravan: { visit_id: string | null; phase: string | null; market_purchases_7d: number }
  lab_market_candidates: Record<string, number>
  bootstrap: {
    applied: boolean
    batch_id: string | null
    created_at: string | null
    preview: AdminEconomyBootstrap | null
  }
  warnings: string[]
}

export interface AdminEconomyBootstrap {
  bootstrap_key: string
  resident_floor_sc: number
  resident_count?: number
  resident_grants?: Array<{
    resident_slug: string
    balance_before_sc: number
    amount_sc: number
    balance_after_sc: number
  }>
  resident_grant_sc?: number
  daily_payroll_sc?: number
  payroll_days?: number
  town_balance_before_sc?: number
  town_target_sc: number
  town_grant_sc: number
  already_applied: boolean
  batch_id?: string
  created_at?: string | null
}

export function getAdminEconomyOperations(token: string): Promise<AdminEconomyOperations> {
  return apiFetch('/admin/economy/operations', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminEconomyBootstrap(token: string): Promise<AdminEconomyBootstrap> {
  return apiFetch('/admin/economy/bootstrap', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function applyAdminEconomyBootstrap(token: string): Promise<AdminEconomyBootstrap> {
  return apiFetch('/admin/economy/bootstrap', {
    method: 'POST',
    body: JSON.stringify({ confirm: true }),
    headers: { Authorization: `Bearer ${token}` },
  })
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

// ─── Admin goal investment pools (E13) ───────────────────────────

export interface AdminInvestmentTopGoal {
  goal_id: string
  title: string
  resident_name: string
  resident_slug: string
  pooled: number
  investments: number
  progress: number
}

export interface AdminInvestmentsResponse {
  total_pooled: number
  investment_count: number
  active_goal_count: number
  investor_count: number
  pool_cap: number
  top_goals: AdminInvestmentTopGoal[]
}

export function getAdminInvestments(token: string): Promise<AdminInvestmentsResponse> {
  return apiFetch('/admin/economy/investments', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Admin gossip / rumor chains (E3) ────────────────────────────

export interface AdminGossipItem {
  id: string
  resident_id: string
  resident_name: string | null
  resident_slug: string | null
  content: string
  importance: number
  hops: number
  distorted: boolean
  origin_memory_id: string | null
  created_at: string | null
}

export interface AdminRumorChainNode {
  id: string
  resident_id: string
  resident_name: string | null
  resident_slug: string | null
  content: string
  hops: number
  distorted: boolean
  importance: number | null
  created_at: string | null
}

export function getAdminGossipRecent(token: string, limit = 50): Promise<{ items: AdminGossipItem[] }> {
  return apiFetch(`/admin/gossip/recent?limit=${limit}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminRumorChain(
  token: string,
  memoryId: string,
): Promise<{ origin_memory_id: string; chain: AdminRumorChainNode[] }> {
  return apiFetch(`/admin/gossip/chains/${encodeURIComponent(memoryId)}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Admin economy series (通胀曲线) ─────────────────────────────

export interface EconomySeriesPoint {
  date: string
  issued: number
  consumed: number
  net: number
}

export function getAdminEconomySeries(token: string, days = 30): Promise<{ days: number; series: EconomySeriesPoint[] }> {
  return apiFetch(`/admin/economy/series?days=${days}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Admin social structure (P2) ─────────────────────────────────

export interface AdminSocialGraphNode {
  id: string
  name: string
  slug: string
  circle_id: string | null
}

export interface AdminSocialGraphEdge {
  a: string
  b: string
  familiarity: number
  affinity: number
}

export interface AdminSocialCircle {
  circle_id: string
  members: string[]
  size: number
  activity: number
}

export interface AdminSocialGraph {
  nodes: AdminSocialGraphNode[]
  edges: AdminSocialGraphEdge[]
  circles: AdminSocialCircle[]
}

export function getAdminSocialGraph(token: string): Promise<AdminSocialGraph> {
  return apiFetch('/admin/social-graph', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Admin offices and policy state (S2) ─────────────────────────

export interface AdminOffice {
  office_key: string
  holder_slug: string | null
  institution: string
  perms_json: Record<string, unknown>
  fill_strategy: string
  term_started_at: string | null
  term_ends_at: string | null
  updated_at: string | null
}

export function getAdminOffices(token: string): Promise<{ offices: AdminOffice[] }> {
  return apiFetch('/admin/offices', {
    headers: { Authorization: `Bearer ${token}` },
  })
}

export interface AdminPolicy {
  key: string
  value: unknown
  tier: 'administrative' | 'simple_majority' | 'absolute_majority' | 'constitutional_core'
  procedure: string
  group: string
  version: number
  updated_by: string | null
  updated_at: string | null
  fiscal_pending: boolean
}

export interface AdminPoliciesResponse {
  enabled: boolean
  matrix: Record<string, { path?: string; [key: string]: unknown }>
  policies: AdminPolicy[]
}

export function getAdminPolicies(token: string): Promise<AdminPoliciesResponse> {
  return apiFetch('/admin/policies', {
    headers: { Authorization: `Bearer ${token}` },
  })
}
