import hre from 'hardhat'
import { upgrades } from '@openzeppelin/hardhat-upgrades/viem'
import { mkdir, writeFile } from 'node:fs/promises'

const connection = await hre.network.create()
const { viem } = connection
const [deployer] = await viem.getWalletClients()
if (!deployer?.account) throw new Error('No deployer account is configured')

const upgradesApi = await upgrades(hre, connection)
const registry = await upgradesApi.deployProxy(
  'SimverseAgentRegistry',
  [deployer.account.address],
  { kind: 'uups', client: deployer },
)
const publicClient = await viem.getPublicClient()
const chainId = await publicClient.getChainId()
const implementation = await upgradesApi.erc1967.getImplementationAddress(registry.address)
const record = {
  chainId,
  proxy: registry.address,
  implementation,
  admin: deployer.account.address,
  contract: 'SimverseAgentRegistry',
  upgradeKind: 'uups',
  deployedAt: new Date().toISOString(),
}
await mkdir('deployments', { recursive: true })
await writeFile(`deployments/${chainId}.json`, `${JSON.stringify(record, null, 2)}\n`, 'utf8')

console.log(`SIMVERSE_AGENT_REGISTRY=${registry.address}`)
console.log(`IMPLEMENTATION=${implementation}`)
console.log(`UPGRADE_ADMIN=${deployer.account.address}`)
console.log(`DEPLOYMENT_RECORD=deployments/${chainId}.json`)
