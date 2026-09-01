import {
  createPublicClient,
  createWalletClient,
  defineChain,
  http,
  keccak256,
  stringToHex,
  parseEther,
} from 'viem'
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts'

const API = process.env.SIMVERSE_E2E_API || 'https://simverse.space/api'
const ORIGIN = process.env.SIMVERSE_E2E_ORIGIN || 'https://simverse.space'
const RPC = process.env.ROBINHOOD_RPC_URL || 'https://rpc.mainnet.chain.robinhood.com'
const REGISTRY = process.env.AGENT_REGISTRY_ADDRESS || '0x24f6f6bE48066cbE0B54d741cd4B52862Bb4b05c'
const CHAIN_ID = 4663

if (process.env.CONFIRM_MAINNET_E2E !== 'YES') {
  throw new Error('Set CONFIRM_MAINNET_E2E=YES to create a permanent mainnet launch-probe Passport.')
}

const deployerKey = process.env.DEPLOYER_PRIVATE_KEY
if (!/^0x[0-9a-fA-F]{64}$/.test(deployerKey || '')) {
  throw new Error('DEPLOYER_PRIVATE_KEY must be a 0x-prefixed 32-byte key.')
}

const chain = defineChain({
  id: CHAIN_ID,
  name: 'Robinhood Chain',
  nativeCurrency: { name: 'Ether', symbol: 'ETH', decimals: 18 },
  rpcUrls: { default: { http: [RPC] } },
})

const registryAbi = [
  { type: 'function', name: 'implementationVersion', stateMutability: 'pure', inputs: [], outputs: [{ type: 'uint256' }] },
  { type: 'function', name: 'nextAgentId', stateMutability: 'view', inputs: [], outputs: [{ type: 'uint256' }] },
  { type: 'function', name: 'agentsOf', stateMutability: 'view', inputs: [{ name: 'owner', type: 'address' }], outputs: [{ type: 'uint256[]' }] },
  { type: 'function', name: 'agentByResident', stateMutability: 'view', inputs: [{ name: 'owner', type: 'address' }, { name: 'residentKey', type: 'bytes32' }], outputs: [{ type: 'uint256' }] },
  { type: 'function', name: 'tokenURI', stateMutability: 'view', inputs: [{ name: 'tokenId', type: 'uint256' }], outputs: [{ type: 'string' }] },
  {
    type: 'function', name: 'agentState', stateMutability: 'view', inputs: [{ name: 'agentId', type: 'uint256' }],
    outputs: [{
      type: 'tuple', components: [
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
]

async function jsonRequest(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      Origin: ORIGIN,
      ...(options.headers || {}),
    },
  })
  const text = await response.text()
  let body
  try { body = text ? JSON.parse(text) : null } catch { body = text }
  if (!response.ok) throw new Error(`${path} returned ${response.status}: ${typeof body === 'string' ? body : JSON.stringify(body)}`)
  return body
}

async function proveWebSocket(jwt) {
  const wsUrl = API.replace(/^http/, 'ws') + '/ws'
  await new Promise((resolve, reject) => {
    const socket = new WebSocket(wsUrl)
    const timer = setTimeout(() => { socket.close(); reject(new Error('WebSocket auth timed out')) }, 15_000)
    socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'auth', token: jwt })))
    socket.addEventListener('message', (event) => {
      try {
        if (JSON.parse(String(event.data)).type === 'auth_ok') {
          clearTimeout(timer); socket.close(); resolve()
        }
      } catch { /* ignore unrelated frames */ }
    })
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('WebSocket connection failed')) })
  })
}

const publicClient = createPublicClient({ chain, transport: http(RPC) })
const deployer = privateKeyToAccount(deployerKey)
const probe = privateKeyToAccount(generatePrivateKey())
const deployerClient = createWalletClient({ account: deployer, chain, transport: http(RPC) })
const probeClient = createWalletClient({ account: probe, chain, transport: http(RPC) })

const version = await publicClient.readContract({ address: REGISTRY, abi: registryAbi, functionName: 'implementationVersion' })
if (version !== 3n) throw new Error(`Registry implementationVersion is ${version}, expected 3.`)
const beforeId = await publicClient.readContract({ address: REGISTRY, abi: registryAbi, functionName: 'nextAgentId' })

const challenge = await jsonRequest('/auth/wallet/challenge', {
  method: 'POST', body: JSON.stringify({ address: probe.address, chain_id: CHAIN_ID }),
})
const signature = await probe.signMessage({ message: challenge.message })
const auth = await jsonRequest('/auth/wallet/verify', {
  method: 'POST',
  body: JSON.stringify({
    address: probe.address,
    message: challenge.message,
    signature,
    nonce: challenge.nonce,
    chain_id: CHAIN_ID,
  }),
})
if (auth.user.wallet_address.toLowerCase() !== probe.address.toLowerCase()) throw new Error('Wallet login address mismatch.')
const headers = { Authorization: `Bearer ${auth.access_token}` }

const templates = await jsonRequest('/sprites/templates', { headers })
if (!Array.isArray(templates) || !templates[0]?.key) throw new Error('No resident sprite template is available.')
const resident = await jsonRequest('/onboarding/create-character', {
  method: 'POST', headers,
  body: JSON.stringify({
    name: `Launch Probe ${new Date().toISOString().slice(0, 10)}`,
    sprite_key: templates[0].key,
    reply_mode: 'auto',
    ability_md: 'Production onboarding and world-entry probe.',
    persona_md: 'A non-custodial launch probe created by the Simverse release check.',
    soul_md: 'Verifies that wallet identity, resident state, and onchain Passport remain connected.',
  }),
})
const metadata = await jsonRequest(`/web3/content/passport-metadata/${encodeURIComponent(resident.id)}`, {
  method: 'POST', headers,
})

const fundingValue = parseEther('0.0005')
const deployerBalance = await publicClient.getBalance({ address: deployer.address })
if (deployerBalance <= fundingValue) throw new Error('Deployer wallet does not have enough ETH for the launch probe.')
const fundingTx = await deployerClient.sendTransaction({ to: probe.address, value: fundingValue })
const fundingReceipt = await publicClient.waitForTransactionReceipt({ hash: fundingTx })
if (fundingReceipt.status !== 'success') throw new Error('Probe funding transaction reverted.')

const residentKey = keccak256(stringToHex(resident.id))
const mintTx = await probeClient.writeContract({
  address: REGISTRY,
  abi: registryAbi,
  functionName: 'createAgentForResident',
  args: [metadata.content_uri, metadata.content_hash, residentKey],
})
const mintReceipt = await publicClient.waitForTransactionReceipt({ hash: mintTx })
if (mintReceipt.status !== 'success') throw new Error('Passport mint transaction reverted.')

const agentId = await publicClient.readContract({
  address: REGISTRY, abi: registryAbi, functionName: 'agentByResident', args: [probe.address, residentKey],
})
const [owned, tokenURI, state] = await Promise.all([
  publicClient.readContract({ address: REGISTRY, abi: registryAbi, functionName: 'agentsOf', args: [probe.address] }),
  publicClient.readContract({ address: REGISTRY, abi: registryAbi, functionName: 'tokenURI', args: [agentId] }),
  publicClient.readContract({ address: REGISTRY, abi: registryAbi, functionName: 'agentState', args: [agentId] }),
])
if (agentId !== beforeId || owned.length !== 1 || owned[0] !== agentId) throw new Error('One-wallet/one-Passport invariant failed.')
if (tokenURI !== metadata.content_uri || state.metadataHash.toLowerCase() !== metadata.content_hash.toLowerCase()) {
  throw new Error('Passport metadata does not match the uploaded public document.')
}

await jsonRequest('/onboarding/passport/confirm', {
  method: 'POST', headers,
  body: JSON.stringify({
    resident_id: resident.id,
    agent_id: agentId.toString(),
    transaction_hash: mintTx,
    metadata_uri: tokenURI,
    metadata_hash: state.metadataHash,
  }),
})
const [onboarding, settings, publicMetadata] = await Promise.all([
  jsonRequest('/onboarding/check', { headers }),
  jsonRequest('/settings', { headers }),
  fetch(tokenURI).then(async (response) => {
    if (!response.ok) throw new Error(`Public metadata returned ${response.status}.`)
    return response.json()
  }),
])
if (onboarding.needs_onboarding || onboarding.passport?.agent_id !== agentId.toString()) throw new Error('Backend Passport binding did not converge.')
if (settings.character?.resident_id !== resident.id) throw new Error('Authenticated game character did not converge.')
if (publicMetadata.simverse?.resident_id !== resident.id) throw new Error('Public Passport metadata is not readable.')
await proveWebSocket(auth.access_token)

let recoveryTransaction = null
try {
  const [remaining, gasPrice] = await Promise.all([
    publicClient.getBalance({ address: probe.address }),
    publicClient.getGasPrice(),
  ])
  const transferGas = 21_000n
  const transferFee = transferGas * gasPrice
  if (remaining > transferFee) {
    recoveryTransaction = await probeClient.sendTransaction({
      to: deployer.address,
      value: remaining - transferFee,
      gas: transferGas,
      gasPrice,
    })
    await publicClient.waitForTransactionReceipt({ hash: recoveryTransaction })
  }
} catch {
  // The core E2E remains valid if a fee-market change prevents sweeping the
  // tiny remainder. Never expose the ephemeral private key to recover it.
}

console.log(JSON.stringify({
  ok: true,
  chainId: CHAIN_ID,
  implementationVersion: Number(version),
  probeAddress: probe.address,
  residentId: resident.id,
  agentId: agentId.toString(),
  fundingTransaction: fundingTx,
  mintTransaction: mintTx,
  recoveryTransaction,
  publicMetadata: tokenURI,
  backendBinding: true,
  websocketAuth: true,
}, null, 2))
