import hre from 'hardhat'
import { upgrades } from '@openzeppelin/hardhat-upgrades/viem'

const connection = await hre.network.create()
const upgradesApi = await upgrades(hre, connection)
await upgradesApi.validateUpgrade(
  'SimverseAgentRegistry',
  'SimverseAgentRegistryV2',
  { kind: 'uups' },
)
console.log('UPGRADE_LAYOUT=VALID')
