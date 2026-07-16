import { apiFetch } from './core'

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

// ─── Admin world events (A2) ─────────────────────────────────────

export interface AdminWorldEvent {
  id: string
  type: string
  title: string
  description: string
  payload_json: Record<string, unknown>
  starts_at: string | null
  ends_at: string | null
  is_active: boolean
  created_by: string | null
}

export function getAdminEvents(token: string): Promise<{ events: AdminWorldEvent[] }> {
  return apiFetch('/admin/events', { headers: { Authorization: `Bearer ${token}` } })
}

export function createAdminEvent(
  token: string,
  data: { type: string; title: string; description: string; starts_at: string; ends_at: string },
): Promise<AdminWorldEvent> {
  return apiFetch('/admin/events', {
    method: 'POST',
    body: JSON.stringify({ ...data, payload_json: {} }),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function deleteAdminEvent(token: string, id: string): Promise<{ ok: boolean }> {
  return apiFetch(`/admin/events/${id}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${token}` },
  })
}

// ─── Admin Lab: run monitor + kill switch (P2) ───────────────────

export interface AdminLabRun {
  id: string
  task_id: string
  researcher_slug: string
  adapter: string
  status: string
  scopes: string[]
  budget_usd_cents: number
  cost_usd_cents: number
  approvals: unknown[]
  error: string | null
  started_at: string | null
  ended_at: string | null
}

export interface AdminLabStatus {
  deploy_enabled: boolean
  runtime_enabled: boolean
  adapter: string
}

export function getAdminLabStatus(token: string): Promise<AdminLabStatus> {
  return apiFetch('/admin/lab/status', { headers: { Authorization: `Bearer ${token}` } })
}

export function setAdminLabKillSwitch(token: string, enabled: boolean): Promise<{ runtime_enabled: boolean }> {
  return apiFetch('/admin/lab/kill-switch', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function getAdminLabRuns(token: string, status?: string): Promise<{ runs: AdminLabRun[] }> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return apiFetch(`/admin/lab/runs${query}`, { headers: { Authorization: `Bearer ${token}` } })
}

export function cancelAdminLabRun(token: string, runId: string): Promise<{ ok: boolean }> {
  return apiFetch(`/admin/lab/runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  })
}
