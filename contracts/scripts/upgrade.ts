import hre from 'hardhat'
import { upgrades } from '@openzeppelin/hardhat-upgrades/viem'
import { mkdir, writeFile } from 'node:fs/promises'

const proxyAddress = process.env.SIMVERSE_AGENT_REGISTRY as `0x${string}` | undefined
if (!proxyAddress) throw new Error('Set SIMVERSE_AGENT_REGISTRY to the UUPS proxy address')

const connection = await hre.network.create()
const { viem } = connection
const upgradesApi = await upgrades(hre, connection)
const [upgrader] = await viem.getWalletClients()
if (!upgrader?.account) throw new Error('No upgrader account is configured')
const publicClient = await viem.getPublicClient()
const networkGasPrice = await publicClient.getGasPrice()
const configuredGasPrice = process.env.ROBINHOOD_GAS_PRICE_WEI
  ? BigInt(process.env.ROBINHOOD_GAS_PRICE_WEI)
  : 0n
// Never submit a legacy gas price below the current base fee. Robinhood Chain's
// base fee is dynamic, so stale hard-coded values can make an otherwise valid
// upgrade fail before broadcast.
const gasPrice = configuredGasPrice > networkGasPrice
  ? configuredGasPrice
  : (networkGasPrice * 125n) / 100n

// Robinhood Chain's public RPC currently rejects automatic deployment gas
// estimation with "contract creation code storage out of gas". Deploy the
// implementation first with a bounded creation limit, then submit the much
// smaller proxy switch separately so the account does not need to reserve the
// creation-sized gas limit twice.
const preparedImplementation = await upgradesApi.prepareUpgrade(
  proxyAddress,
  'SimverseAgentRegistryV3',
  {
    kind: 'uups',
    client: upgrader,
    gas: 3_200_000n,
    gasPrice,
  },
)
const upgraded = await upgradesApi.upgradeProxy(
  proxyAddress,
  'SimverseAgentRegistryV3',
  {
    kind: 'uups',
    client: upgrader,
    gas: 250_000n,
    gasPrice,
    redeployImplementation: 'never',
  },
)
const chainId = await publicClient.getChainId()
const implementation = await upgradesApi.erc1967.getImplementationAddress(upgraded.address)
await mkdir('deployments', { recursive: true })
await writeFile(`deployments/${chainId}.json`, `${JSON.stringify({
  chainId,
  proxy: upgraded.address,
  implementation,
  admin: upgrader?.account?.address ?? null,
  contract: 'SimverseAgentRegistryV3',
  upgradeKind: 'uups',
  upgradedAt: new Date().toISOString(),
}, null, 2)}\n`, 'utf8')

console.log(`SIMVERSE_AGENT_REGISTRY=${upgraded.address}`)
console.log(`IMPLEMENTATION=${implementation}`)
console.log(`PREPARED_IMPLEMENTATION=${preparedImplementation}`)
console.log('IMPLEMENTATION_VERSION=3')
console.log(`DEPLOYMENT_RECORD=deployments/${chainId}.json`)
