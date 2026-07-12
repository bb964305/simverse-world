import { API_BASE, apiFetch, getToken } from './core'

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
