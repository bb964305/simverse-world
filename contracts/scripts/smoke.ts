import hre from 'hardhat'

const address = process.env.SIMVERSE_AGENT_REGISTRY as `0x${string}` | undefined
if (!address) throw new Error('Set SIMVERSE_AGENT_REGISTRY to the UUPS proxy address')

const connection = await hre.network.create()
const { viem } = connection
const [owner] = await viem.getWalletClients()
if (!owner?.account) throw new Error('No local wallet client is available')

const registry = await viem.getContractAt('SimverseAgentRegistry', address, {
  client: { wallet: owner },
})
const publicClient = await viem.getPublicClient()

const metadataHash = `0x${'11'.repeat(32)}` as const
const artifactHash = `0x${'22'.repeat(32)}` as const
const memoryHash = `0x${'33'.repeat(32)}` as const
const saveHash = `0x${'44'.repeat(32)}` as const

const createTx = await registry.write.createAgent(['simverse://smoke/agent.json', metadataHash])
await publicClient.waitForTransactionReceipt({ hash: createTx })
const ids = await registry.read.agentsOf([owner.account.address])
const agentId = ids.at(-1)
if (agentId === undefined) throw new Error('Agent Passport was not created')

for (const hash of [
  await registry.write.publishVersion([agentId, 'simverse://smoke/training.bin', artifactHash, artifactHash]),
  await registry.write.anchorMemory([agentId, 'simverse://smoke/memory.json', memoryHash]),
  await registry.write.anchorSave([agentId, 'simverse://smoke/save.json', saveHash]),
]) {
  await publicClient.waitForTransactionReceipt({ hash })
}

const state = await registry.read.agentState([agentId])
if (state.version !== 1n || state.memoryRevision !== 1n || state.saveRevision !== 1n) {
  throw new Error('Unexpected persisted Agent state')
}

console.log(`SMOKE_AGENT_ID=${agentId}`)
console.log(`SMOKE_OWNER=${owner.account.address}`)
console.log(`SMOKE_VERSIONS=${state.version}/${state.memoryRevision}/${state.saveRevision}`)
