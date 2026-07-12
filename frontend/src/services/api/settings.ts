import { apiFetch } from './core'

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
