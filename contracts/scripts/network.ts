import hre from 'hardhat'
import { formatEther } from 'viem'

const connection = await hre.network.create()
const { viem } = connection
const [deployer] = await viem.getWalletClients()
if (!deployer?.account) throw new Error('No deployer account is configured')

const publicClient = await viem.getPublicClient()
const [chainId, balance, gasPrice, blockNumber] = await Promise.all([
  publicClient.getChainId(),
  publicClient.getBalance({ address: deployer.account.address }),
  publicClient.getGasPrice(),
  publicClient.getBlockNumber(),
])

const registryAddress = process.env.SIMVERSE_AGENT_REGISTRY as `0x${string}` | undefined
const registryVersion = registryAddress
  ? Number(await publicClient.readContract({
      address: registryAddress,
      abi: [{ type: 'function', name: 'implementationVersion', stateMutability: 'view', inputs: [], outputs: [{ type: 'uint256' }] }],
      functionName: 'implementationVersion',
    }))
  : null

console.log(JSON.stringify({
  network: connection.networkName,
  chainId,
  blockNumber: blockNumber.toString(),
  deployer: deployer.account.address,
  balanceETH: formatEther(balance),
  gasPriceWei: gasPrice.toString(),
  registryAddress: registryAddress ?? null,
  registryVersion,
}, null, 2))
