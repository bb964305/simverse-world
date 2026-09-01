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
    robinhoodTestnet: {
      type: 'http',
      chainType: 'generic',
      chainId: 46630,
      url: configVariable('ROBINHOOD_TESTNET_RPC_URL', {
        default: 'https://rpc.testnet.chain.robinhood.com',
      }),
      accounts: [configVariable('ROBINHOOD_PRIVATE_KEY')],
    },
    robinhood: {
      type: 'http',
      chainType: 'generic',
      chainId: 4663,
      url: configVariable('ROBINHOOD_RPC_URL', {
        default: 'https://rpc.mainnet.chain.robinhood.com',
      }),
      accounts: [configVariable('ROBINHOOD_PRIVATE_KEY')],
    },
  },
})
