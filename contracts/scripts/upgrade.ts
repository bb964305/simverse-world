import hre from 'hardhat'
import { upgrades } from '@openzeppelin/hardhat-upgrades/viem'
import { mkdir, writeFile } from 'node:fs/promises'

const proxyAddress = process.env.SIMVERSE_AGENT_REGISTRY as `0x${string}` | undefined
if (!proxyAddress) throw new Error('Set SIMVERSE_AGENT_REGISTRY to the UUPS proxy address')

const connection = await hre.network.create()
const { viem } = connection
const upgradesApi = await upgrades(hre, connection)
const upgraded = await upgradesApi.upgradeProxy(
  proxyAddress,
  'SimverseAgentRegistryV2',
  { kind: 'uups' },
)
const publicClient = await viem.getPublicClient()
const chainId = await publicClient.getChainId()
const implementation = await upgradesApi.erc1967.getImplementationAddress(upgraded.address)
const [upgrader] = await viem.getWalletClients()
await mkdir('deployments', { recursive: true })
await writeFile(`deployments/${chainId}.json`, `${JSON.stringify({
  chainId,
  proxy: upgraded.address,
  implementation,
  admin: upgrader?.account?.address ?? null,
  contract: 'SimverseAgentRegistryV2',
  upgradeKind: 'uups',
  upgradedAt: new Date().toISOString(),
}, null, 2)}\n`, 'utf8')

console.log(`SIMVERSE_AGENT_REGISTRY=${upgraded.address}`)
console.log(`IMPLEMENTATION=${implementation}`)
console.log('IMPLEMENTATION_VERSION=2')
console.log(`DEPLOYMENT_RECORD=deployments/${chainId}.json`)
