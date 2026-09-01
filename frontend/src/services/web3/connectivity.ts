import { API_BASE } from '../api/core'
import { AGENT_REGISTRY_ADDRESS } from './agentRegistry'
import { configuredChainId, configuredChainName, configuredRpcUrl } from './wallet'

export interface ConnectivityItem {
  ok: boolean
  detail: string
}

export interface ConnectivityReport {
  api: ConnectivityItem
  runtime: ConnectivityItem
  chain: ConnectivityItem
  contract: ConnectivityItem
  checkedAt: string
}

const NEXT_AGENT_ID_ABI = [{
  type: 'function', name: 'nextAgentId', stateMutability: 'view', inputs: [],
  outputs: [{ name: '', type: 'uint256' }],
}] as const

function failed(reason: unknown): ConnectivityItem {
  return { ok: false, detail: reason instanceof Error ? reason.message : String(reason) }
}

async function checkApi(signal: AbortSignal): Promise<ConnectivityItem> {
  try {
    const response = await fetch(`${API_BASE}/health`, { signal, headers: { Accept: 'application/json' } })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return { ok: true, detail: `${API_BASE}/health` }
  } catch (reason) {
    return failed(reason)
  }
}

export async function checkAgentRuntime(signal: AbortSignal): Promise<ConnectivityItem> {
  try {
    const response = await fetch(`${API_BASE}/health/loops`, { signal, headers: { Accept: 'application/json' } })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const body = await response.json() as { status?: string; loops?: { agent?: { state?: string; last_beat?: string } } }
    if (body.status !== 'ok' || body.loops?.agent?.state !== 'ok') throw new Error(`Agent loop ${body.loops?.agent?.state ?? 'unknown'}`)
    return { ok: true, detail: `Agent loop online · ${body.loops.agent.last_beat ?? 'heartbeat live'}` }
  } catch (reason) {
    return failed(reason)
  }
}

async function checkChainAndContract(): Promise<Pick<ConnectivityReport, 'chain' | 'contract'>> {
  const viem = await import('viem')
  const client = viem.createPublicClient({ transport: viem.http(configuredRpcUrl(), { timeout: 10_000 }) })

  const chainPromise = client.getChainId().then((chainId) => {
    if (chainId !== configuredChainId()) throw new Error(`Chain ID ${chainId}, expected ${configuredChainId()}`)
    return { ok: true, detail: `${configuredChainName()} · ${chainId}` } satisfies ConnectivityItem
  }).catch(failed)

  const contractPromise = (async (): Promise<ConnectivityItem> => {
    if (!/^0x[0-9a-fA-F]{40}$/.test(AGENT_REGISTRY_ADDRESS)) throw new Error('Agent Registry address is not configured')
    const [bytecode, nextAgentId] = await Promise.all([
      client.getBytecode({ address: AGENT_REGISTRY_ADDRESS }),
      client.readContract({ address: AGENT_REGISTRY_ADDRESS, abi: NEXT_AGENT_ID_ABI, functionName: 'nextAgentId' }),
    ])
    if (!bytecode || bytecode === '0x') throw new Error('No proxy bytecode at configured address')
    return { ok: true, detail: `Proxy online · next Agent #${nextAgentId.toString()}` }
  })().catch(failed)

  const [chain, contract] = await Promise.all([chainPromise, contractPromise])
  return { chain, contract }
}

export async function checkProductionConnectivity(signal?: AbortSignal): Promise<ConnectivityReport> {
  const timeout = AbortSignal.timeout(12_000)
  const mergedSignal = signal ? AbortSignal.any([signal, timeout]) : timeout
  const [api, runtime, onchain] = await Promise.all([
    checkApi(mergedSignal),
    checkAgentRuntime(mergedSignal),
    checkChainAndContract().catch((reason) => ({ chain: failed(reason), contract: failed(reason) })),
  ])
  return { api, runtime, ...onchain, checkedAt: new Date().toISOString() }
}
