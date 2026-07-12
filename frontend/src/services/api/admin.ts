import { apiFetch } from './core'

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
