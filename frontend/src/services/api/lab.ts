import { apiFetch, apiFetchResponse } from './core'

// Lab (experiment building) player API — mirrors backend routers/lab.py (§8).

export interface LabResearcher {
  slug: string
  name: string
  tier: string | null
  skills: string[]
  busy: boolean
  avg_rating: number
}

export interface LabTask {
  id: string
  issuer_user_id: string
  researcher_slug: string | null
  title: string
  brief_md: string
  scopes: string[]
  reward_sc: number
  platform_fee_sc: number
  deliverable_kind: string
  status: string
  accepted_run_id: string | null
  reject_count: number
  result_summary_md: string | null
  deadline_at: string | null
  review_deadline_at: string | null
  created_at: string | null
  completed_at: string | null
}

// Server-authoritative approval projection (backend acl.approval_projection).
// The v1 GET /lab/runs/{id} returns these fields; the flag-off legacy path
// returns only id/tool/summary/status, so the projection fields are optional and
// the frontend must never invent authority the server didn't grant.
export interface LabApproval {
  approval_id?: string          // v1 projection id
  id?: string                   // legacy approvals_json id
  action_id?: string | null
  allowed_actions?: string[]    // present only on the v1 projection
  can_decide?: boolean
  decision_scope?: string | null
  status?: string               // pending|approved|denied|expired
  tool?: string | null
  summary?: string
  preview?: unknown
}

export interface LabRun {
  id: string
  task_id: string
  researcher_slug: string
  adapter: string
  status: string
  scopes: string[]
  model_tier?: string | null
  model_name?: string | null
  model_policy_version?: string | null
  resource_cpu_cores?: number | null
  resource_memory_mb?: number | null
  budget_usd_cents: number
  cost_usd_cents: number
  approvals: LabApproval[]
  error: string | null
  started_at: string | null
  ended_at: string | null
}

export interface LabRunStep {
  id: string
  run_id: string
  seq: number
  phase: string
  tool: string | null
  summary: string
  payload: Record<string, unknown>
  created_at: string | null
}

export interface LabArtifact {
  id: string
  run_id: string
  task_id: string
  kind: string
  title: string
  unlocked: boolean
  created_at: string | null
  // Manifest metadata (always present, read-only; P3/T5).
  sha256?: string | null
  byte_size?: number
  producer_action_id?: string | null
  provenance?: string | null
  scan_status?: string | null
  verification_status?: string | null
  retention_hold?: boolean
  provider_artifact_id?: string | null
  required?: boolean
  content_type?: string | null
  original_filename?: string | null
  expected_sha256?: string | null
  declared_byte_size?: number | null
  storage_status?: string | null
  scanned_at?: string | null
  released_at?: string | null
  expires_at?: string | null
}

export interface CreateLabTaskInput {
  title: string
  brief_md: string
  scopes: string[]
  reward_sc: number
  deliverable_kind?: string
  researcher_slug?: string | null
  deadline_hours?: number | null
}

export interface LabTaskQuote {
  reward_sc: number
  platform_fee_sc: number
  total_hold_sc: number
  minimum_reward_sc: number
  eligible: boolean
  adapter: string
  available_scopes: string[]
  unsupported_scopes: string[]
  model_tier: 'low' | 'high'
  model_name: string
  model_policy_version: string
  resource_cpu_cores: number
  resource_memory_mb: number
  budget_usd_cents: number
  pro_min_reward_sc: number
}

export function listLabResearchers(): Promise<{ researchers: LabResearcher[] }> {
  return apiFetch('/lab/researchers')
}

export function createLabTask(input: CreateLabTaskInput): Promise<LabTask> {
  return apiFetch('/lab/tasks', { method: 'POST', body: JSON.stringify(input) })
}

export function quoteLabTask(input: Pick<CreateLabTaskInput, 'reward_sc' | 'scopes'>): Promise<LabTaskQuote> {
  return apiFetch('/lab/quote', { method: 'POST', body: JSON.stringify(input) })
}

export function getLabTasks(scope: 'mine' | 'open' = 'mine'): Promise<{ tasks: LabTask[] }> {
  return apiFetch(`/lab/tasks?scope=${scope}`)
}

export function getLabTask(id: string): Promise<{ task: LabTask; run: LabRun | null; artifacts: LabArtifact[] }> {
  return apiFetch(`/lab/tasks/${encodeURIComponent(id)}`)
}

export function cancelLabTask(id: string): Promise<LabTask> {
  return apiFetch(`/lab/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
}

export function acceptLabResult(id: string): Promise<LabTask> {
  return apiFetch(`/lab/tasks/${encodeURIComponent(id)}/accept-result`, { method: 'POST' })
}

export function rejectLabResult(id: string): Promise<LabTask> {
  return apiFetch(`/lab/tasks/${encodeURIComponent(id)}/reject-result`, { method: 'POST' })
}

export function getLabRun(id: string): Promise<LabRun> {
  return apiFetch(`/lab/runs/${encodeURIComponent(id)}`)
}

export function getLabRunSteps(id: string, after = 0): Promise<{ steps: LabRunStep[] }> {
  return apiFetch(`/lab/runs/${encodeURIComponent(id)}/steps?after=${after}`)
}

export function getLabArtifact(id: string): Promise<LabArtifact> {
  return apiFetch(`/lab/artifacts/${encodeURIComponent(id)}`)
}

export async function downloadLabArtifact(
  id: string,
  disposition: 'attachment' | 'inline' = 'attachment',
): Promise<{ blob: Blob; filename: string }> {
  const resp = await apiFetchResponse(
    `/lab/artifacts/${encodeURIComponent(id)}/download?disposition=${disposition}`,
  )
  const header = resp.headers.get('Content-Disposition') || ''
  const encoded = header.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  const quoted = header.match(/filename="([^"]+)"/i)?.[1]
  const filename = encoded
    ? decodeURIComponent(encoded)
    : quoted || `artifact-${id}`
  return { blob: await resp.blob(), filename }
}

export function respondLabApproval(runId: string, approvalId: string, decision: boolean): Promise<{ ok: boolean }> {
  return apiFetch(`/lab/runs/${encodeURIComponent(runId)}/approval`, {
    method: 'POST',
    body: JSON.stringify({ approval_id: approvalId, decision }),
  })
}

// World locations — static + dynamic-overlay merged snapshot (P3, spec §7/§9).
export interface WorldLocation {
  slug: string
  name: string | null
  type: string | null
  role: string | null
  bounds: number[] | null
  center: number[] | null
  entrance: number[] | null
  description: string | null
  boosted_actions: string[]
  dynamic: boolean
}

export function getWorldLocations(): Promise<{ locations: WorldLocation[] }> {
  return apiFetch('/world/locations')
}
