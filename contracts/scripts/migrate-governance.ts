import hre from 'hardhat'
import { getAddress, keccak256, stringToHex, zeroAddress } from 'viem'
import { mkdir, writeFile } from 'node:fs/promises'

if (process.env.CONFIRM_GOVERNANCE_MIGRATION !== 'YES') {
  throw new Error('Set CONFIRM_GOVERNANCE_MIGRATION=YES after reviewing the multisig and delay.')
}

const proxyAddress = process.env.SIMVERSE_AGENT_REGISTRY as `0x${string}` | undefined
const multisigRaw = process.env.GOVERNANCE_MULTISIG_ADDRESS
if (!proxyAddress) throw new Error('Set SIMVERSE_AGENT_REGISTRY to the UUPS proxy address')
if (!multisigRaw) throw new Error('Set GOVERNANCE_MULTISIG_ADDRESS to a deployed multisig')

const minimumDelay = BigInt(process.env.GOVERNANCE_DELAY_SECONDS || '86400')
if (minimumDelay < 3600n) throw new Error('GOVERNANCE_DELAY_SECONDS must be at least 3600')
const multisig = getAddress(multisigRaw)
const executor = getAddress(process.env.GOVERNANCE_EXECUTOR_ADDRESS || multisig)
const worldWriter = process.env.WORLD_WRITER_ADDRESS
  ? getAddress(process.env.WORLD_WRITER_ADDRESS)
  : null

const connection = await hre.network.create()
const { viem } = connection
const [deployer] = await viem.getWalletClients()
if (!deployer?.account) throw new Error('No governance migration account is configured')
if (multisig === deployer.account.address) {
  throw new Error('GOVERNANCE_MULTISIG_ADDRESS must not be the current deployer EOA')
}

const publicClient = await viem.getPublicClient()
const chainId = await publicClient.getChainId()
const registry = await viem.getContractAt('SimverseAgentRegistryV3', proxyAddress, {
  client: { wallet: deployer },
})
const defaultAdminRole = zeroAddress
const upgraderRole = keccak256(stringToHex('UPGRADER_ROLE'))
const worldWriterRole = keccak256(stringToHex('WORLD_WRITER_ROLE'))

for (const role of [defaultAdminRole, upgraderRole, worldWriterRole]) {
  if (!await registry.read.hasRole([role, deployer.account.address])) {
    throw new Error(`Migration account is missing required registry role ${role}`)
  }
}

const timelock = await viem.deployContract('SimverseGovernanceTimelock', [
  minimumDelay,
  [multisig],
  [executor],
], { client: deployer })

async function wait(hash: `0x${string}`) {
  const receipt = await publicClient.waitForTransactionReceipt({ hash })
  if (receipt.status !== 'success') throw new Error(`Governance transaction reverted: ${hash}`)
}

for (const role of [defaultAdminRole, upgraderRole]) {
  await wait(await registry.write.grantRole([role, timelock.address]))
}
if (worldWriter) await wait(await registry.write.grantRole([worldWriterRole, worldWriter]))

// Remove operational and upgrade authority before admin, then drop admin last.
await wait(await registry.write.renounceRole([upgraderRole, deployer.account.address]))
await wait(await registry.write.renounceRole([worldWriterRole, deployer.account.address]))
await wait(await registry.write.renounceRole([defaultAdminRole, deployer.account.address]))

const checks = await Promise.all([
  registry.read.hasRole([defaultAdminRole, timelock.address]),
  registry.read.hasRole([upgraderRole, timelock.address]),
  registry.read.hasRole([defaultAdminRole, deployer.account.address]),
  registry.read.hasRole([upgraderRole, deployer.account.address]),
  registry.read.hasRole([worldWriterRole, deployer.account.address]),
])
if (!checks[0] || !checks[1] || checks[2] || checks[3] || checks[4]) {
  throw new Error('Post-migration registry role invariant failed')
}

const record = {
  chainId,
  registry: proxyAddress,
  timelock: timelock.address,
  minimumDelaySeconds: minimumDelay.toString(),
  proposerMultisig: multisig,
  executor,
  worldWriter,
  previousAdmin: deployer.account.address,
  migratedAt: new Date().toISOString(),
}
await mkdir('deployments', { recursive: true })
await writeFile(`deployments/governance-${chainId}.json`, `${JSON.stringify(record, null, 2)}\n`, 'utf8')
console.log(JSON.stringify(record, null, 2))
