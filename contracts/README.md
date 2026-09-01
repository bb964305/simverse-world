# Simverse Agent Contracts

Upgradeable, non-economic contracts for wallet-owned Simverse Agents on Robinhood Chain.

- `SimverseAgentRegistry.sol`: UUPS Agent Passport, training/upload provenance, memory/save chains, and world proofs.
- `SimverseAgentRegistryV2.sol`: upgrade/state-preservation test implementation.
- `test/SimverseAgentRegistry.t.sol`: Solidity behavior, authorization, soulbound, and UUPS tests.
- `scripts/deploy.ts`: deploy a proxy and write a chain deployment record.
- `scripts/network.ts`: verify the selected Robinhood RPC, chain ID, deployer, balance, and gas price without writing onchain.
- `scripts/upgrade.ts`: upgrade the existing proxy and update its record.
- `scripts/smoke.ts`: perform real JSON-RPC create/train/memory/save writes.

There is deliberately no ERC-20, sale, staking, market, royalty, payment, or withdrawal logic.

Targets:

- Robinhood Chain Testnet: chain ID `46630`, `npm run deploy:robinhood-testnet`
- Robinhood Chain: chain ID `4663`, `npm run deploy:robinhood`

See [`../docs/WEB3_GUIDE.md`](../docs/WEB3_GUIDE.md) for the complete workflow.
