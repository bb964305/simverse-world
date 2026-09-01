// SPDX-License-Identifier: MIT
pragma solidity ^0.8.34;

import {SimverseAgentRegistryV2} from "./SimverseAgentRegistryV2.sol";

/// @notice Production V3: exactly one wallet, one resident, one Passport.
/// @dev No new storage is introduced. V1 Passports can still be linked once;
/// V2-linked wallets cannot mint for arbitrary additional resident keys.
contract SimverseAgentRegistryV3 is SimverseAgentRegistryV2 {
    error WalletAlreadyRegistered(uint256 agentId);
    error AmbiguousLegacyWallet();

    function createAgentForResident(
        string calldata metadataURI,
        bytes32 metadataHash,
        bytes32 residentKey
    ) external override returns (uint256 agentId, bool created) {
        if (residentKey == bytes32(0)) revert ResidentKeyRequired();

        agentId = _agentByResident[msg.sender][residentKey];
        if (agentId != 0) return (agentId, false);

        uint256[] storage owned = _agentsByOwner[msg.sender];
        if (owned.length != 0) {
            agentId = owned[0];
            if (owned.length != 1) revert AmbiguousLegacyWallet();
            bytes32 linkedKey = _residentKeyByAgent[agentId];
            if (linkedKey != bytes32(0)) revert WalletAlreadyRegistered(agentId);

            _agentByResident[msg.sender][residentKey] = agentId;
            _residentKeyByAgent[agentId] = residentKey;
            emit AgentResidentLinked(agentId, msg.sender, residentKey);
            return (agentId, false);
        }

        agentId = _createAgent(msg.sender, metadataURI, metadataHash);
        _agentByResident[msg.sender][residentKey] = agentId;
        _residentKeyByAgent[agentId] = residentKey;
        emit AgentResidentLinked(agentId, msg.sender, residentKey);
        return (agentId, true);
    }

    function linkExistingAgent(uint256 agentId, bytes32 residentKey) external override {
        if (residentKey == bytes32(0)) revert ResidentKeyRequired();
        _requireAgentOwner(agentId);

        uint256[] storage owned = _agentsByOwner[msg.sender];
        if (owned.length != 1 || owned[0] != agentId) revert AmbiguousLegacyWallet();
        uint256 existing = _agentByResident[msg.sender][residentKey];
        if (existing != 0 && existing != agentId) revert ResidentAlreadyLinked(existing);
        bytes32 linkedKey = _residentKeyByAgent[agentId];
        if (linkedKey != bytes32(0) && linkedKey != residentKey) revert AgentAlreadyLinked(linkedKey);
        if (existing == agentId && linkedKey == residentKey) return;

        _agentByResident[msg.sender][residentKey] = agentId;
        _residentKeyByAgent[agentId] = residentKey;
        emit AgentResidentLinked(agentId, msg.sender, residentKey);
    }

    function implementationVersion() external pure override returns (uint256) {
        return 3;
    }
}
