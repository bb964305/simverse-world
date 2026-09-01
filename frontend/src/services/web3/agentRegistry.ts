import type { Locale } from '../locale'
import { keccak256, toHex } from 'viem'
import { getWeb3Runtime, requireWalletAccount } from './wallet'

const addressValue = import.meta.env.VITE_AGENT_REGISTRY_ADDRESS || ''
export const AGENT_REGISTRY_ADDRESS = addressValue as `0x${string}`

export interface AgentChainState {
  metadataHash: `0x${string}`
  latestArtifactHash: `0x${string}`
  trainingRoot: `0x${string}`
  latestMemoryHash: `0x${string}`
  latestSaveHash: `0x${string}`
  version: bigint
  memoryRevision: bigint
  saveRevision: bigint
  createdAt: bigint
  updatedAt: bigint
}

export interface ContentAnchor {
  contentHash: `0x${string}`
  parentHash: `0x${string}`
  contentURI: string
  revision: bigint
  recordedAt: bigint
}

export interface OwnedAgent {
  id: bigint
  uri: string
  state: AgentChainState
  residentKey?: `0x${string}`
  worldProofCount: bigint
}

const registryAbi = [
  {
    type: 'function', name: 'agentsOf', stateMutability: 'view', inputs: [{ name: 'owner', type: 'address' }],
    outputs: [{ name: '', type: 'uint256[]' }],
  },
  {
    type: 'function', name: 'tokenURI', stateMutability: 'view', inputs: [{ name: 'tokenId', type: 'uint256' }],
    outputs: [{ name: '', type: 'string' }],
  },
  {
    type: 'function', name: 'agentState', stateMutability: 'view', inputs: [{ name: 'agentId', type: 'uint256' }],
    outputs: [{
      name: '', type: 'tuple', components: [
        { name: 'metadataHash', type: 'bytes32' }, { name: 'latestArtifactHash', type: 'bytes32' },
        { name: 'trainingRoot', type: 'bytes32' }, { name: 'latestMemoryHash', type: 'bytes32' },
        { name: 'latestSaveHash', type: 'bytes32' }, { name: 'version', type: 'uint64' },
        { name: 'memoryRevision', type: 'uint64' }, { name: 'saveRevision', type: 'uint64' },
        { name: 'createdAt', type: 'uint64' }, { name: 'updatedAt', type: 'uint64' },
      ],
    }],
  },
  {
    type: 'function', name: 'createAgentForResident', stateMutability: 'nonpayable',
    inputs: [{ name: 'metadataURI', type: 'string' }, { name: 'metadataHash', type: 'bytes32' }, { name: 'residentKey', type: 'bytes32' }],
    outputs: [{ name: 'agentId', type: 'uint256' }, { name: 'created', type: 'bool' }],
  },
  {
    type: 'function', name: 'agentByResident', stateMutability: 'view',
    inputs: [{ name: 'owner', type: 'address' }, { name: 'residentKey', type: 'bytes32' }], outputs: [{ name: 'agentId', type: 'uint256' }],
  },
  {
    type: 'function', name: 'residentKeyOf', stateMutability: 'view',
    inputs: [{ name: 'agentId', type: 'uint256' }], outputs: [{ name: 'residentKey', type: 'bytes32' }],
  },
  {
    type: 'function', name: 'updateMetadata', stateMutability: 'nonpayable',
    inputs: [{ name: 'agentId', type: 'uint256' }, { name: 'metadataURI', type: 'string' }, { name: 'metadataHash', type: 'bytes32' }], outputs: [],
  },
  {
    type: 'function', name: 'publishVersion', stateMutability: 'nonpayable',
    inputs: [{ name: 'agentId', type: 'uint256' }, { name: 'artifactURI', type: 'string' }, { name: 'artifactHash', type: 'bytes32' }, { name: 'trainingRoot', type: 'bytes32' }],
    outputs: [{ name: 'version', type: 'uint64' }],
  },
  {
    type: 'function', name: 'anchorMemory', stateMutability: 'nonpayable',
    inputs: [{ name: 'agentId', type: 'uint256' }, { name: 'contentURI', type: 'string' }, { name: 'contentHash', type: 'bytes32' }],
    outputs: [{ name: 'revision', type: 'uint64' }],
  },
  {
    type: 'function', name: 'anchorSave', stateMutability: 'nonpayable',
    inputs: [{ name: 'agentId', type: 'uint256' }, { name: 'contentURI', type: 'string' }, { name: 'contentHash', type: 'bytes32' }],
    outputs: [{ name: 'revision', type: 'uint64' }],
  },
  {
    type: 'function', name: 'recordWorldProof', stateMutability: 'nonpayable',
    inputs: [{ name: 'agentId', type: 'uint256' }, { name: 'kind', type: 'bytes32' }, { name: 'dataHash', type: 'bytes32' }, { name: 'worldRevision', type: 'uint64' }],
    outputs: [{ name: 'proofId', type: 'uint256' }],
  },
  {
    type: 'function', name: 'worldProofCount', stateMutability: 'view', inputs: [{ name: 'agentId', type: 'uint256' }],
    outputs: [{ name: '', type: 'uint256' }],
  },
  {
    type: 'function', name: 'saveAnchorCount', stateMutability: 'view', inputs: [{ name: 'agentId', type: 'uint256' }],
    outputs: [{ name: '', type: 'uint256' }],
  },
  {
    type: 'function', name: 'saveAnchor', stateMutability: 'view',
    inputs: [{ name: 'agentId', type: 'uint256' }, { name: 'anchorId', type: 'uint256' }],
    outputs: [{
      name: '', type: 'tuple', components: [
        { name: 'contentHash', type: 'bytes32' }, { name: 'parentHash', type: 'bytes32' },
        { name: 'contentURI', type: 'string' }, { name: 'revision', type: 'uint64' },
        { name: 'recordedAt', type: 'uint64' },
      ],
    }],
  },
] as const

export function registryConfigured(): boolean {
  return /^0x[0-9a-fA-F]{40}$/.test(AGENT_REGISTRY_ADDRESS)
}

export function residentKeyFor(residentId: string): `0x${string}` {
  return keccak256(toHex(residentId))
}

function requireRegistry(): `0x${string}` {
  if (!registryConfigured()) throw new Error('VITE_AGENT_REGISTRY_ADDRESS is not configured')
  return AGENT_REGISTRY_ADDRESS
}

export async function loadOwnedAgents(owner: `0x${string}`): Promise<OwnedAgent[]> {
  const { config, core } = await getWeb3Runtime()
  const address = requireRegistry()
  const ids = await core.readContract(config, { address, abi: registryAbi, functionName: 'agentsOf', args: [owner] })
  return Promise.all(ids.map(async (id) => {
    const [uri, state, residentKey, worldProofCount] = await Promise.all([
      core.readContract(config, { address, abi: registryAbi, functionName: 'tokenURI', args: [id] }),
      core.readContract(config, { address, abi: registryAbi, functionName: 'agentState', args: [id] }),
      core.readContract(config, { address, abi: registryAbi, functionName: 'residentKeyOf', args: [id] }),
      core.readContract(config, { address, abi: registryAbi, functionName: 'worldProofCount', args: [id] }),
    ])
    return { id, uri, state: state as AgentChainState, residentKey, worldProofCount }
  }))
}

export async function loadAgentForResident(owner: `0x${string}`, residentId: string) {
  const { config, core } = await getWeb3Runtime()
  const address = requireRegistry()
  const key = residentKeyFor(residentId)
  const id = await core.readContract(config, {
    address, abi: registryAbi, functionName: 'agentByResident', args: [owner, key],
  })
  if (id === 0n) return null
  const [uri, state] = await Promise.all([
    core.readContract(config, { address, abi: registryAbi, functionName: 'tokenURI', args: [id] }),
    core.readContract(config, { address, abi: registryAbi, functionName: 'agentState', args: [id] }),
  ])
  return { id, uri, state: state as AgentChainState, residentKey: key }
}

export async function loadLatestSaveAnchor(agentId: bigint): Promise<ContentAnchor | null> {
  const { config, core } = await getWeb3Runtime()
  const address = requireRegistry()
  const count = await core.readContract(config, {
    address, abi: registryAbi, functionName: 'saveAnchorCount', args: [agentId],
  })
  if (count === 0n) return null
  const anchor = await core.readContract(config, {
    address, abi: registryAbi, functionName: 'saveAnchor', args: [agentId, count - 1n],
  })
  return anchor as ContentAnchor
}

async function write(
  locale: Locale,
  expectedAccount: `0x${string}`,
  functionName: 'createAgentForResident' | 'updateMetadata' | 'publishVersion' | 'anchorMemory' | 'anchorSave' | 'recordWorldProof',
  args: readonly unknown[],
): Promise<`0x${string}`> {
  const address = requireRegistry()
  const { address: account, runtime } = await requireWalletAccount(locale)
  if (account.toLowerCase() !== expectedAccount.toLowerCase()) {
    throw new Error(locale === 'en' ? 'Connected wallet does not match this session.' : '当前钱包与登录身份不一致')
  }
  const hash = await runtime.core.writeContract(runtime.config, {
    account,
    address,
    abi: registryAbi,
    functionName,
    args,
  } as never)
  const receipt = await runtime.core.waitForTransactionReceipt(runtime.config, { hash })
  if (receipt.status !== 'success') throw new Error('Contract transaction reverted')
  return hash
}

export function createAgentPassport(locale: Locale, account: `0x${string}`, residentId: string, uri: string, hash: `0x${string}`) {
  return write(locale, account, 'createAgentForResident', [uri, hash, residentKeyFor(residentId)])
}

export function updateAgentMetadata(locale: Locale, account: `0x${string}`, agentId: bigint, uri: string, hash: `0x${string}`) {
  return write(locale, account, 'updateMetadata', [agentId, uri, hash])
}

export function publishTrainingVersion(locale: Locale, account: `0x${string}`, agentId: bigint, uri: string, hash: `0x${string}`, trainingRoot: `0x${string}`) {
  return write(locale, account, 'publishVersion', [agentId, uri, hash, trainingRoot])
}

export function anchorMemory(locale: Locale, account: `0x${string}`, agentId: bigint, uri: string, hash: `0x${string}`) {
  return write(locale, account, 'anchorMemory', [agentId, uri, hash])
}

export function anchorSave(locale: Locale, account: `0x${string}`, agentId: bigint, uri: string, hash: `0x${string}`) {
  return write(locale, account, 'anchorSave', [agentId, uri, hash])
}

export function recordWorldProof(
  locale: Locale,
  account: `0x${string}`,
  agentId: bigint,
  kind: `0x${string}`,
  hash: `0x${string}`,
  worldRevision: bigint,
) {
  return write(locale, account, 'recordWorldProof', [agentId, kind, hash, worldRevision])
}
