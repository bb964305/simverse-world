// SPDX-License-Identifier: MIT
pragma solidity ^0.8.34;

import {SimverseAgentRegistry} from "./SimverseAgentRegistry.sol";

/// @notice Production V2: one wallet-owned Passport per Simverse resident.
/// @dev New mappings are appended after every V1 storage slot, preserving the
/// deployed UUPS proxy layout. The resident key is keccak256(UTF-8 resident id).
contract SimverseAgentRegistryV2 is SimverseAgentRegistry {
    error ResidentKeyRequired();
    error LegacyCreationDisabled();
    error ResidentAlreadyLinked(uint256 agentId);
    error AgentAlreadyLinked(bytes32 residentKey);

    event AgentResidentLinked(
        uint256 indexed agentId,
        address indexed owner,
        bytes32 indexed residentKey
    );

    mapping(address owner => mapping(bytes32 residentKey => uint256 agentId))
        internal _agentByResident;
    mapping(uint256 agentId => bytes32 residentKey) internal _residentKeyByAgent;

    /// @notice Legacy unscoped creation is disabled after the V2 upgrade.
    function createAgent(
        string calldata,
        bytes32
    ) external pure override returns (uint256) {
        revert LegacyCreationDisabled();
    }

    /// @notice Create exactly one Passport for a wallet/resident pair.
    /// @return agentId Existing or newly created Agent id.
    /// @return created True only when this transaction minted the Passport.
    function createAgentForResident(
        string calldata metadataURI,
        bytes32 metadataHash,
        bytes32 residentKey
    ) external virtual returns (uint256 agentId, bool created) {
        if (residentKey == bytes32(0)) revert ResidentKeyRequired();
        agentId = _agentByResident[msg.sender][residentKey];
        if (agentId != 0) return (agentId, false);

        agentId = _createAgent(msg.sender, metadataURI, metadataHash);
        _agentByResident[msg.sender][residentKey] = agentId;
        _residentKeyByAgent[agentId] = residentKey;
        emit AgentResidentLinked(agentId, msg.sender, residentKey);
        return (agentId, true);
    }

    /// @notice One-time migration helper for a Passport minted before V2.
    function linkExistingAgent(uint256 agentId, bytes32 residentKey) external virtual {
        if (residentKey == bytes32(0)) revert ResidentKeyRequired();
        _requireAgentOwner(agentId);
        uint256 existing = _agentByResident[msg.sender][residentKey];
        if (existing != 0 && existing != agentId) revert ResidentAlreadyLinked(existing);
        bytes32 linkedKey = _residentKeyByAgent[agentId];
        if (linkedKey != bytes32(0) && linkedKey != residentKey) revert AgentAlreadyLinked(linkedKey);
        if (existing == agentId && linkedKey == residentKey) return;

        _agentByResident[msg.sender][residentKey] = agentId;
        _residentKeyByAgent[agentId] = residentKey;
        emit AgentResidentLinked(agentId, msg.sender, residentKey);
    }

    function agentByResident(address owner, bytes32 residentKey) external view returns (uint256) {
        return _agentByResident[owner][residentKey];
    }

    function residentKeyOf(uint256 agentId) external view returns (bytes32) {
        _requireOwned(agentId);
        return _residentKeyByAgent[agentId];
    }

    function implementationVersion() external pure virtual returns (uint256) {
        return 2;
    }
}
