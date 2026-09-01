import hardhatToolboxViem from '@nomicfoundation/hardhat-toolbox-viem'
import hardhatUpgrades from '@openzeppelin/hardhat-upgrades/viem'
import { configVariable, defineConfig } from 'hardhat/config'

export default defineConfig({
  plugins: [hardhatToolboxViem, hardhatUpgrades],
  solidity: {
    version: '0.8.34',
    settings: {
      optimizer: { enabled: true, runs: 200 },
      evmVersion: 'cancun',
    },
  },
  networks: {
    localhost: {
      type: 'http',
      chainType: 'op',
      url: 'http://127.0.0.1:8545',
    },
    baseSepolia: {
      type: 'http',
      chainType: 'op',
      chainId: 84532,
      url: configVariable('BASE_SEPOLIA_RPC_URL', {
        default: 'https://sepolia.base.org',
      }),
      accounts: [configVariable('BASE_SEPOLIA_PRIVATE_KEY')],
    },
  },
})
