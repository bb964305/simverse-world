# Simverse Agent Contracts

Upgradeable, non-economic contracts for wallet-owned Simverse Agents.

- `SimverseAgentRegistry.sol`: UUPS Agent Passport, training/upload provenance, memory/save chains, and world proofs.
- `SimverseAgentRegistryV2.sol`: upgrade/state-preservation test implementation.
- `test/SimverseAgentRegistry.t.sol`: Solidity behavior, authorization, soulbound, and UUPS tests.
- `scripts/deploy.ts`: deploy a proxy and write a chain deployment record.
- `scripts/upgrade.ts`: upgrade the existing proxy and update its record.
- `scripts/smoke.ts`: perform real JSON-RPC create/train/memory/save writes.

There is deliberately no ERC-20, sale, staking, market, royalty, payment, or withdrawal logic.

See [`../docs/WEB3_GUIDE.md`](../docs/WEB3_GUIDE.md) for the complete workflow.
