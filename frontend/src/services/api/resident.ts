import { apiFetch } from './core'

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
  home_location_id?: string | null
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

// ─── Player Position API ─────────────────────────────────────────

export function updatePlayerPosition(tileX: number, tileY: number): Promise<{ tile_x: number; tile_y: number }> {
  return apiFetch('/residents/player/position', {
    method: 'PUT',
    body: JSON.stringify({ tile_x: tileX, tile_y: tileY }),
  })
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

// ── Soul card (C1, public share) ────────────────────────────────────
// GET /residents/{slug}/card — no auth; mirrors backend resident_card().
export interface ResidentCardData {
  slug: string
  name: string
  soul_excerpt: string
  sbti_type: string | null
  sbti_name: string | null
  star_rating: number
  portrait_url: string | null
  total_conversations: number
  signature_reflection: string | null
}

export function getResidentCard(slug: string): Promise<ResidentCardData> {
  return apiFetch(`/residents/${encodeURIComponent(slug)}/card`)
}

// ─── Exploration codex (E8) ──────────────────────────────────────

export interface CodexLocation {
  location_id: string
  name: string
  visited: boolean
  visit_count: number
  secret_found: boolean
  has_secret: boolean
  lore: string | null
  bounds: [number, number, number, number]
}

export function getExplorationCodex(): Promise<{ total: number; visited: number; locations: CodexLocation[] }> {
  return apiFetch('/exploration/me')
}
