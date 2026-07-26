export interface ResidentSpriteIdentity {
  id: string
  sprite_key: string
  sprite_url?: string | null
  sprite_content_hash?: string | null
  sprite_generation_run_id?: string | null
}

export interface ResidentSpriteUpdatedMessage {
  type: 'sprite_updated'
  resident_id: string
  slug?: string
  sprite_key: string
  sprite_url: string | null
  content_hash: string | null
  run_id?: string | null
}

export type ResidentTextureLoadDecision = 'apply' | 'keep-current' | 'discard-stale'

export const STATIC_RESIDENT_ATLAS_JSON_URL = `/assets/village/agents/sprite.json?v=${__RESIDENT_SPRITE_ASSET_VERSION__}`

export function decideResidentTextureLoad(
  loaded: boolean,
  isShutdown: boolean,
  desiredTextureKey: string | undefined,
  loadedTextureKey: string,
): ResidentTextureLoadDecision {
  if (isShutdown || desiredTextureKey !== loadedTextureKey) return 'discard-stale'
  return loaded ? 'apply' : 'keep-current'
}

function shortSafeToken(value: string): string {
  const safe = value.toLowerCase().replace(/[^a-z0-9_-]/g, '')
  return safe.slice(0, 64) || 'unversioned'
}

export function staticResidentSpriteUrl(spriteKey: string): string {
  return `/assets/village/agents/${encodeURIComponent(spriteKey)}/texture.png?v=${__RESIDENT_SPRITE_ASSET_VERSION__}`
}

export function staticResidentPortraitUrl(spriteKey: string): string {
  return `/assets/village/agents/${encodeURIComponent(spriteKey)}/portrait.png?v=${__RESIDENT_SPRITE_ASSET_VERSION__}`
}

export function resolveResidentSpriteUrl(
  spriteUrl: string | null | undefined,
  apiBase: string,
  contentHash?: string | null,
): string | null {
  if (!spriteUrl) return null
  try {
    const resolved = new URL(spriteUrl, apiBase)
    if (resolved.protocol !== 'http:' && resolved.protocol !== 'https:') return null
    if (contentHash) resolved.searchParams.set('v', contentHash)
    return resolved.toString()
  } catch {
    return null
  }
}

export function residentTextureKey(resident: ResidentSpriteIdentity): string {
  if (!resident.sprite_url) return resident.sprite_key
  const residentToken = shortSafeToken(resident.id)
  const contentToken = shortSafeToken(
    resident.sprite_content_hash || resident.sprite_generation_run_id || 'unversioned',
  )
  return `resident-sprite-${residentToken}-${contentToken}`
}

export function parseResidentSpriteUpdatedMessage(
  value: Record<string, unknown>,
): ResidentSpriteUpdatedMessage | null {
  if (
    value.type !== 'sprite_updated'
    || typeof value.resident_id !== 'string'
    || typeof value.sprite_key !== 'string'
    || (value.sprite_url !== null && typeof value.sprite_url !== 'string')
    || (value.content_hash !== null && typeof value.content_hash !== 'string')
  ) return null

  return {
    type: 'sprite_updated',
    resident_id: value.resident_id,
    slug: typeof value.slug === 'string' ? value.slug : undefined,
    sprite_key: value.sprite_key,
    sprite_url: value.sprite_url,
    content_hash: value.content_hash,
    run_id: typeof value.run_id === 'string' ? value.run_id : null,
  }
}
