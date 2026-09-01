import type { Locale } from '../locale'
import { createAgentPassport } from './agentRegistry'
import { uploadWeb3Content } from './content'

export interface PassportResident {
  id: string
  slug: string
  name: string
  sprite_key: string
  district?: string
  status?: string
  star_rating?: number
  meta_json?: Record<string, unknown> | null
}

export async function registerResidentOnchain(
  locale: Locale,
  wallet: `0x${string}`,
  resident: PassportResident,
): Promise<`0x${string}`> {
  const metadata = {
    schema: 'simverse-agent-passport-v1',
    name: resident.name,
    description: `Wallet-owned Simverse resident ${resident.slug}`,
    external_url: `${window.location.origin}/profile`,
    owner: wallet,
    simverse: {
      resident_id: resident.id,
      slug: resident.slug,
      district: resident.district ?? 'free',
      status: resident.status ?? 'idle',
      sprite_key: resident.sprite_key,
      star_rating: resident.star_rating ?? 0,
      profile: resident.meta_json ?? null,
    },
  }
  const blob = new Blob([JSON.stringify(metadata, null, 2)], { type: 'application/json' })
  const content = await uploadWeb3Content(blob, `${resident.slug}-agent-passport.json`)
  return createAgentPassport(locale, wallet, content.content_uri, content.content_hash)
}
