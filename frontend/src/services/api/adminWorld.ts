import { apiFetch, apiFetchResponse } from './core'

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
  sprite_url?: string | null
  sprite_content_hash?: string | null
  sprite_generation_run_id?: string | null
  portrait_url?: string | null
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

// ─── Admin resident sprite generation & review ──────────────────

export const RESIDENT_SPRITE_REVIEW_KEYS = [
  'identity_consistency',
  'down_direction',
  'left_direction',
  'right_direction',
  'up_direction',
  'walk_animation',
  'transparent_background',
  'limited_palette',
  'phaser_preview',
] as const

export type ResidentSpriteReviewKey = typeof RESIDENT_SPRITE_REVIEW_KEYS[number]
export type ResidentSpriteChecklist = Record<ResidentSpriteReviewKey, boolean>

export interface ResidentSpriteGenerationRequest {
  asset_key: string
  display_name: string
  appearance: string
  gender: 'male' | 'female' | 'neutral'
  age_group: 'young' | 'adult' | 'elder'
  vibe: string
  tags: string[]
  direction_policy: 'mirror_right' | 'generate_right'
  model: string
  anchor_quality: 'medium'
  strip_quality: 'high'
  palette_colors: 32
  prompt_version: 'resident-sprite-v1'
  algorithm_version: 'resident-atlas-v2'
  max_strip_generations: 3
}

export interface AdminResidentSpriteRun {
  id: string
  resident_id: string
  run_id: string
  status: string
  direction_policy: 'mirror_right' | 'generate_right'
  generation_request_json: ResidentSpriteGenerationRequest
  capability_receipt_id: string | null
  lease_owner: string | null
  lease_expires_at: string | null
  attempts: number
  retry_of_run_id: string | null
  manifest_path: string | null
  candidate_texture_path: string | null
  candidate_portrait_path: string | null
  candidate_texture_url?: string | null
  candidate_portrait_url?: string | null
  candidate_texture_sha256: string | null
  candidate_portrait_sha256: string | null
  published_texture_sha256: string | null
  published_portrait_sha256: string | null
  request_count: number
  estimated_cost_usd: number | null
  review_evidence_json: Record<string, unknown> | null
  review_checklist_json: Partial<ResidentSpriteChecklist> | null
  review_notes: string | null
  reviewed_by: string | null
  reviewed_at: string | null
  rejection_reason: string | null
  published_by: string | null
  published_at: string | null
  rolled_back_by: string | null
  rolled_back_at: string | null
  rollback_reason: string | null
  error_code: string | null
  error_message: string | null
  version: number
  created_at: string
  updated_at: string
}

export interface AdminResidentSpriteRunsResponse {
  items: AdminResidentSpriteRun[]
  total: number
  page: number
  per_page: number
}

export interface CreateResidentSpriteRunRequest {
  resident_id: string
  appearance?: string
  gender?: 'male' | 'female' | 'neutral'
  age_group?: 'young' | 'adult' | 'elder'
  vibe?: string
  tags?: string[]
  direction_policy?: 'mirror_right' | 'generate_right'
}

function spriteRunPath(runId: string, action = ''): string {
  return `/admin/resident-sprites/${encodeURIComponent(runId)}${action ? `/${action}` : ''}`
}

export function getAdminResidentSpriteRuns(
  token: string,
  params: { status?: string; resident_id?: string; page?: number; per_page?: number } = {},
  signal?: AbortSignal,
): Promise<AdminResidentSpriteRunsResponse> {
  const qs = new URLSearchParams()
  if (params.status) qs.set('status', params.status)
  if (params.resident_id) qs.set('resident_id', params.resident_id)
  if (params.page != null) qs.set('page', String(params.page))
  if (params.per_page != null) qs.set('per_page', String(params.per_page))
  const query = qs.size > 0 ? `?${qs.toString()}` : ''
  return apiFetch(`/admin/resident-sprites${query}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  })
}

export function getAdminResidentSpriteRun(
  token: string,
  runId: string,
  signal?: AbortSignal,
): Promise<AdminResidentSpriteRun> {
  return apiFetch(spriteRunPath(runId), {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  })
}

export function createAdminResidentSpriteRun(
  token: string,
  data: CreateResidentSpriteRunRequest,
): Promise<AdminResidentSpriteRun> {
  return apiFetch('/admin/resident-sprites', {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function progressAdminResidentSpriteRun(
  token: string,
  runId: string,
  data: { action: 'resume' | 'retry'; expected_version: number },
): Promise<AdminResidentSpriteRun> {
  return apiFetch(spriteRunPath(runId, 'progress'), {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function reviewAdminResidentSpriteRun(
  token: string,
  runId: string,
  data: {
    expected_version: number
    evidence: Record<string, unknown>
    checklist: ResidentSpriteChecklist
    notes: string
  },
): Promise<AdminResidentSpriteRun> {
  return apiFetch(spriteRunPath(runId, 'review'), {
    method: 'PUT',
    body: JSON.stringify(data),
    headers: { Authorization: `Bearer ${token}` },
  })
}

function mutateAdminResidentSpriteRun(
  token: string,
  runId: string,
  action: 'approve' | 'publish',
  expectedVersion: number,
): Promise<AdminResidentSpriteRun> {
  return apiFetch(spriteRunPath(runId, action), {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion }),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function approveAdminResidentSpriteRun(
  token: string,
  runId: string,
  expectedVersion: number,
): Promise<AdminResidentSpriteRun> {
  return mutateAdminResidentSpriteRun(token, runId, 'approve', expectedVersion)
}

export function publishAdminResidentSpriteRun(
  token: string,
  runId: string,
  expectedVersion: number,
): Promise<AdminResidentSpriteRun> {
  return mutateAdminResidentSpriteRun(token, runId, 'publish', expectedVersion)
}

export function rejectAdminResidentSpriteRun(
  token: string,
  runId: string,
  expectedVersion: number,
  reason: string,
): Promise<AdminResidentSpriteRun> {
  return apiFetch(spriteRunPath(runId, 'reject'), {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion, reason }),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export function rollbackAdminResidentSpriteRun(
  token: string,
  runId: string,
  expectedVersion: number,
  reason: string,
): Promise<AdminResidentSpriteRun> {
  return apiFetch(spriteRunPath(runId, 'rollback'), {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion, reason }),
    headers: { Authorization: `Bearer ${token}` },
  })
}

export async function getAdminResidentSpriteCandidate(
  token: string,
  runId: string,
  kind: 'texture' | 'portrait',
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await apiFetchResponse(spriteRunPath(runId, `candidate/${kind}`), {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  })
  return response.blob()
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

// ─── Admin World governance: proposal review (P3) ────────────────

export interface WorldProposal {
  id: string
  origin: string
  origin_ref: string | null
  author_slug: string | null
  kind: string
  title: string
  rationale_md: string
  patch: Record<string, unknown>
  cost_sc: number
  status: string
  risk_level: string
  reviewer_id: string | null
  review_note: string | null
  applied_at: string | null
  reverted_at: string | null
  created_at: string | null
  preview?: { kind: string; conflicts?: string[]; adds_location?: string }
}

export function getAdminProposals(token: string, status?: string): Promise<{ proposals: WorldProposal[] }> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return apiFetch(`/admin/world/proposals${query}`, { headers: { Authorization: `Bearer ${token}` } })
}

export function getAdminProposal(token: string, id: string): Promise<WorldProposal> {
  return apiFetch(`/admin/world/proposals/${encodeURIComponent(id)}`, { headers: { Authorization: `Bearer ${token}` } })
}

export function approveAdminProposal(token: string, id: string, note = ''): Promise<WorldProposal> {
  return apiFetch(`/admin/world/proposals/${encodeURIComponent(id)}/approve`, {
    method: 'POST', body: JSON.stringify({ note }), headers: { Authorization: `Bearer ${token}` },
  })
}

export function rejectAdminProposal(token: string, id: string, note = ''): Promise<WorldProposal> {
  return apiFetch(`/admin/world/proposals/${encodeURIComponent(id)}/reject`, {
    method: 'POST', body: JSON.stringify({ note }), headers: { Authorization: `Bearer ${token}` },
  })
}

export function revertAdminProposal(token: string, id: string): Promise<WorldProposal> {
  return apiFetch(`/admin/world/proposals/${encodeURIComponent(id)}/revert`, {
    method: 'POST', headers: { Authorization: `Bearer ${token}` },
  })
}
