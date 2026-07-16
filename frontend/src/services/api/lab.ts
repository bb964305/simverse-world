import { apiFetch } from './core'

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

export interface LabRun {
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
  uri?: string | null
  text_md?: string | null
  meta?: Record<string, unknown>
  created_at: string | null
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

export function listLabResearchers(): Promise<{ researchers: LabResearcher[] }> {
  return apiFetch('/lab/researchers')
}

export function createLabTask(input: CreateLabTaskInput): Promise<LabTask> {
  return apiFetch('/lab/tasks', { method: 'POST', body: JSON.stringify(input) })
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

export function respondLabApproval(runId: string, approvalId: string, decision: boolean): Promise<{ ok: boolean }> {
  return apiFetch(`/lab/runs/${encodeURIComponent(runId)}/approval`, {
    method: 'POST',
    body: JSON.stringify({ approval_id: approvalId, decision }),
  })
}
