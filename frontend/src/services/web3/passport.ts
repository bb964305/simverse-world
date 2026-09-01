import type { Locale } from '../locale'
import { confirmResidentPassport } from '../api/resident'
import { getToken } from '../api/core'
import { createAgentPassport, loadAgentForResident, updateAgentMetadata } from './agentRegistry'
import { createPassportMetadata } from './content'

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
): Promise<{ agentId: bigint; transaction: `0x${string}` | null }> {
  const token = getToken()
  if (!token) throw new Error('Wallet session required')
  let agent = await loadAgentForResident(wallet, resident.id)
  let transaction: `0x${string}` | null = null
  if (!agent) {
    const content = await createPassportMetadata(resident.id)
    transaction = await createAgentPassport(locale, wallet, resident.id, content.content_uri, content.content_hash)
    agent = await loadAgentForResident(wallet, resident.id)
    if (!agent) throw new Error(locale === 'en' ? 'Passport transaction confirmed but resident link was not found.' : '交易已确认，但没有找到居民对应的 Passport。')
  }
  await confirmResidentPassport(token, {
    resident_id: resident.id,
    agent_id: agent.id.toString(),
    transaction_hash: transaction,
    metadata_uri: agent.uri,
    metadata_hash: agent.state.metadataHash,
  })
  return { agentId: agent.id, transaction }
}

export async function syncResidentMetadataOnchain(
  locale: Locale,
  wallet: `0x${string}`,
  resident: PassportResident,
  agentId: bigint,
): Promise<`0x${string}`> {
  const token = getToken()
  if (!token) throw new Error('Wallet session required')
  const content = await createPassportMetadata(resident.id)
  const transaction = await updateAgentMetadata(locale, wallet, agentId, content.content_uri, content.content_hash)
  const agent = await loadAgentForResident(wallet, resident.id)
  if (!agent || agent.id !== agentId) throw new Error(locale === 'en' ? 'Resident Passport link changed during metadata update.' : '更新元数据时，居民与 Passport 的绑定发生了变化。')
  await confirmResidentPassport(token, {
    resident_id: resident.id,
    agent_id: agent.id.toString(),
    transaction_hash: null,
    metadata_uri: agent.uri,
    metadata_hash: agent.state.metadataHash,
  })
  return transaction
}
