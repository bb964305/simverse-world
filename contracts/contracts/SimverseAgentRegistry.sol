// SPDX-License-Identifier: MIT
pragma solidity ^0.8.34;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {AccessControlUpgradeable} from "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import {ERC721Upgradeable} from "@openzeppelin/contracts-upgradeable/token/ERC721/ERC721Upgradeable.sol";
import {ERC721URIStorageUpgradeable} from "@openzeppelin/contracts-upgradeable/token/ERC721/extensions/ERC721URIStorageUpgradeable.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";

/// @title Simverse Agent Registry
/// @notice Upgradeable, non-transferable AI Agent passports plus provenance proofs.
/// @dev No ERC-20, payments, royalties, marketplace, or token economics exist here.
contract SimverseAgentRegistry is
    Initializable,
    ERC721URIStorageUpgradeable,
    AccessControlUpgradeable,
    UUPSUpgradeable
{
    struct AgentState {
        bytes32 metadataHash;
        bytes32 latestArtifactHash;
        bytes32 trainingRoot;
        bytes32 latestMemoryHash;
        bytes32 latestSaveHash;
        uint64 version;
        uint64 memoryRevision;
        uint64 saveRevision;
        uint64 createdAt;
        uint64 updatedAt;
    }

    /// @notice An immutable pointer to content stored outside the chain.
    /// @dev The content hash is the source of truth; the URI may point to IPFS,
    /// Arweave, or the Simverse upload API. Private content should be encrypted.
    struct ContentAnchor {
        bytes32 contentHash;
        bytes32 parentHash;
        string contentURI;
        uint64 revision;
        uint64 recordedAt;
    }

    struct WorldProof {
        bytes32 kind;
        bytes32 dataHash;
        uint64 worldRevision;
        uint64 recordedAt;
    }

    error Soulbound();
    error NotAgentOwner();
    error EmptyURI();
    error URITooLong();
    error EmptyHash();
    error InvalidAdmin();

    bytes32 public constant UPGRADER_ROLE = keccak256("UPGRADER_ROLE");
    bytes32 public constant WORLD_WRITER_ROLE = keccak256("WORLD_WRITER_ROLE");
    uint256 public constant MAX_URI_BYTES = 512;

    uint256 private _nextAgentId;
    mapping(uint256 agentId => AgentState state) private _agentState;
    mapping(address owner => uint256[] agentIds) private _agentsByOwner;
    mapping(uint256 agentId => WorldProof[] proofs) private _worldProofs;
    mapping(uint256 agentId => ContentAnchor[] anchors) private _memoryAnchors;
    mapping(uint256 agentId => ContentAnchor[] anchors) private _saveAnchors;
    uint256[38] private __gap;

    event AgentCreated(
        uint256 indexed agentId,
        address indexed owner,
        string metadataURI,
        bytes32 indexed metadataHash
    );
    event AgentMetadataUpdated(
        uint256 indexed agentId,
        string metadataURI,
        bytes32 indexed metadataHash
    );
    event AgentVersionPublished(
        uint256 indexed agentId,
        uint64 indexed version,
        string artifactURI,
        bytes32 indexed artifactHash,
        bytes32 trainingRoot
    );
    event WorldProofRecorded(
        uint256 indexed agentId,
        uint256 indexed proofId,
        bytes32 indexed kind,
        bytes32 dataHash,
        uint64 worldRevision
    );
    event MemoryAnchored(
        uint256 indexed agentId,
        uint64 indexed revision,
        bytes32 indexed contentHash,
        bytes32 parentHash,
        string contentURI
    );
    event SaveAnchored(
        uint256 indexed agentId,
        uint64 indexed revision,
        bytes32 indexed contentHash,
        bytes32 parentHash,
        string contentURI
    );

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address initialAdmin) public initializer {
        if (initialAdmin == address(0)) revert InvalidAdmin();
        __ERC721_init("Simverse Agent Passport", "SVA");
        __ERC721URIStorage_init();
        __AccessControl_init();

        _nextAgentId = 1;
        _grantRole(DEFAULT_ADMIN_ROLE, initialAdmin);
        _grantRole(UPGRADER_ROLE, initialAdmin);
        _grantRole(WORLD_WRITER_ROLE, initialAdmin);
    }

    function createAgent(
        string calldata metadataURI,
        bytes32 metadataHash
    ) external returns (uint256 agentId) {
        _validateURI(metadataURI);
        if (metadataHash == bytes32(0)) revert EmptyHash();

        agentId = _nextAgentId++;
        uint64 timestamp = uint64(block.timestamp);
        _agentState[agentId] = AgentState({
            metadataHash: metadataHash,
            latestArtifactHash: bytes32(0),
            trainingRoot: bytes32(0),
            latestMemoryHash: bytes32(0),
            latestSaveHash: bytes32(0),
            version: 0,
            memoryRevision: 0,
            saveRevision: 0,
            createdAt: timestamp,
            updatedAt: timestamp
        });
        _agentsByOwner[msg.sender].push(agentId);
        _mint(msg.sender, agentId);
        _setTokenURI(agentId, metadataURI);
        emit AgentCreated(agentId, msg.sender, metadataURI, metadataHash);
    }

    function updateMetadata(
        uint256 agentId,
        string calldata metadataURI,
        bytes32 metadataHash
    ) external {
        _requireAgentOwner(agentId);
        _validateURI(metadataURI);
        if (metadataHash == bytes32(0)) revert EmptyHash();

        AgentState storage state = _agentState[agentId];
        state.metadataHash = metadataHash;
        state.updatedAt = uint64(block.timestamp);
        _setTokenURI(agentId, metadataURI);
        emit AgentMetadataUpdated(agentId, metadataURI, metadataHash);
    }

    /// @notice Anchor a training/model/upload version without storing private data on-chain.
    function publishVersion(
        uint256 agentId,
        string calldata artifactURI,
        bytes32 artifactHash,
        bytes32 trainingRoot
    ) external returns (uint64 version) {
        _requireAgentOwner(agentId);
        _validateURI(artifactURI);
        if (artifactHash == bytes32(0)) revert EmptyHash();

        AgentState storage state = _agentState[agentId];
        version = ++state.version;
        state.latestArtifactHash = artifactHash;
        state.trainingRoot = trainingRoot;
        state.updatedAt = uint64(block.timestamp);
        emit AgentVersionPublished(agentId, version, artifactURI, artifactHash, trainingRoot);
    }

    /// @notice Anchor a new append-only memory snapshot for an Agent.
    /// @dev The owner can write directly. A trusted world writer can write after
    /// authenticating the wallet session and validating the content server-side.
    function anchorMemory(
        uint256 agentId,
        string calldata contentURI,
        bytes32 contentHash
    ) external returns (uint64 revision) {
        _requireAgentOwnerOrWorldWriter(agentId);
        _validateURI(contentURI);
        if (contentHash == bytes32(0)) revert EmptyHash();

        AgentState storage state = _agentState[agentId];
        bytes32 parentHash = state.latestMemoryHash;
        revision = ++state.memoryRevision;
        state.latestMemoryHash = contentHash;
        state.updatedAt = uint64(block.timestamp);
        _memoryAnchors[agentId].push(ContentAnchor({
            contentHash: contentHash,
            parentHash: parentHash,
            contentURI: contentURI,
            revision: revision,
            recordedAt: uint64(block.timestamp)
        }));
        emit MemoryAnchored(agentId, revision, contentHash, parentHash, contentURI);
    }

    /// @notice Anchor a new append-only game save snapshot for an Agent.
    function anchorSave(
        uint256 agentId,
        string calldata contentURI,
        bytes32 contentHash
    ) external returns (uint64 revision) {
        _requireAgentOwnerOrWorldWriter(agentId);
        _validateURI(contentURI);
        if (contentHash == bytes32(0)) revert EmptyHash();

        AgentState storage state = _agentState[agentId];
        bytes32 parentHash = state.latestSaveHash;
        revision = ++state.saveRevision;
        state.latestSaveHash = contentHash;
        state.updatedAt = uint64(block.timestamp);
        _saveAnchors[agentId].push(ContentAnchor({
            contentHash: contentHash,
            parentHash: parentHash,
            contentURI: contentURI,
            revision: revision,
            recordedAt: uint64(block.timestamp)
        }));
        emit SaveAnchored(agentId, revision, contentHash, parentHash, contentURI);
    }

    /// @notice Anchor an authenticated game achievement or world interaction.
    function recordWorldProof(
        uint256 agentId,
        bytes32 kind,
        bytes32 dataHash,
        uint64 worldRevision
    ) external onlyRole(WORLD_WRITER_ROLE) returns (uint256 proofId) {
        _requireOwned(agentId);
        if (kind == bytes32(0) || dataHash == bytes32(0)) revert EmptyHash();
        proofId = _worldProofs[agentId].length;
        _worldProofs[agentId].push(WorldProof({
            kind: kind,
            dataHash: dataHash,
            worldRevision: worldRevision,
            recordedAt: uint64(block.timestamp)
        }));
        emit WorldProofRecorded(agentId, proofId, kind, dataHash, worldRevision);
    }

    function nextAgentId() external view returns (uint256) {
        return _nextAgentId;
    }

    function agentState(uint256 agentId) external view returns (AgentState memory) {
        _requireOwned(agentId);
        return _agentState[agentId];
    }

    function agentsOf(address owner) external view returns (uint256[] memory) {
        return _agentsByOwner[owner];
    }

    function worldProofCount(uint256 agentId) external view returns (uint256) {
        _requireOwned(agentId);
        return _worldProofs[agentId].length;
    }

    function worldProof(uint256 agentId, uint256 proofId) external view returns (WorldProof memory) {
        _requireOwned(agentId);
        return _worldProofs[agentId][proofId];
    }

    function memoryAnchorCount(uint256 agentId) external view returns (uint256) {
        _requireOwned(agentId);
        return _memoryAnchors[agentId].length;
    }

    function memoryAnchor(
        uint256 agentId,
        uint256 anchorId
    ) external view returns (ContentAnchor memory) {
        _requireOwned(agentId);
        return _memoryAnchors[agentId][anchorId];
    }

    function saveAnchorCount(uint256 agentId) external view returns (uint256) {
        _requireOwned(agentId);
        return _saveAnchors[agentId].length;
    }

    function saveAnchor(
        uint256 agentId,
        uint256 anchorId
    ) external view returns (ContentAnchor memory) {
        _requireOwned(agentId);
        return _saveAnchors[agentId][anchorId];
    }

    function approve(address, uint256) public pure override(ERC721Upgradeable, IERC721) {
        revert Soulbound();
    }

    function setApprovalForAll(address, bool) public pure override(ERC721Upgradeable, IERC721) {
        revert Soulbound();
    }

    function supportsInterface(
        bytes4 interfaceId
    ) public view override(ERC721URIStorageUpgradeable, AccessControlUpgradeable) returns (bool) {
        return super.supportsInterface(interfaceId);
    }

    function _update(
        address to,
        uint256 tokenId,
        address auth
    ) internal override returns (address) {
        address from = _ownerOf(tokenId);
        if (from != address(0) && to != address(0)) revert Soulbound();
        return super._update(to, tokenId, auth);
    }

    function _authorizeUpgrade(address) internal override onlyRole(UPGRADER_ROLE) {}

    function _requireAgentOwner(uint256 agentId) private view {
        if (ownerOf(agentId) != msg.sender) revert NotAgentOwner();
    }

    function _requireAgentOwnerOrWorldWriter(uint256 agentId) private view {
        address owner = ownerOf(agentId);
        if (owner != msg.sender && !hasRole(WORLD_WRITER_ROLE, msg.sender)) {
            revert NotAgentOwner();
        }
    }

    function _validateURI(string calldata uri) private pure {
        uint256 length = bytes(uri).length;
        if (length == 0) revert EmptyURI();
        if (length > MAX_URI_BYTES) revert URITooLong();
    }
}
