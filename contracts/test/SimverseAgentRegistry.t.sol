// SPDX-License-Identifier: MIT
pragma solidity ^0.8.34;

import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {SimverseAgentRegistry} from "../contracts/SimverseAgentRegistry.sol";
import {SimverseAgentRegistryV2} from "../contracts/mocks/SimverseAgentRegistryV2.sol";

contract AgentActor {
    function create(
        SimverseAgentRegistry registry,
        string calldata uri,
        bytes32 digest
    ) external returns (uint256) {
        return registry.createAgent(uri, digest);
    }

    function publish(
        SimverseAgentRegistry registry,
        uint256 agentId,
        string calldata uri,
        bytes32 artifactHash,
        bytes32 trainingRoot
    ) external returns (uint64) {
        return registry.publishVersion(agentId, uri, artifactHash, trainingRoot);
    }

    function remember(
        SimverseAgentRegistry registry,
        uint256 agentId,
        string calldata uri,
        bytes32 contentHash
    ) external returns (uint64) {
        return registry.anchorMemory(agentId, uri, contentHash);
    }

    function save(
        SimverseAgentRegistry registry,
        uint256 agentId,
        string calldata uri,
        bytes32 contentHash
    ) external returns (uint64) {
        return registry.anchorSave(agentId, uri, contentHash);
    }

    function tryTransfer(
        SimverseAgentRegistry registry,
        address to,
        uint256 agentId
    ) external returns (bool) {
        try registry.transferFrom(address(this), to, agentId) {
            return true;
        } catch {
            return false;
        }
    }

    function tryUpgrade(
        SimverseAgentRegistry registry,
        address implementation
    ) external returns (bool) {
        try registry.upgradeToAndCall(implementation, "") {
            return true;
        } catch {
            return false;
        }
    }
}

contract SimverseAgentRegistryTest {
    SimverseAgentRegistry private registry;
    AgentActor private alice;
    AgentActor private bob;

    function setUp() public {
        SimverseAgentRegistry implementation = new SimverseAgentRegistry();
        ERC1967Proxy proxy = new ERC1967Proxy(
            address(implementation),
            abi.encodeCall(SimverseAgentRegistry.initialize, (address(this)))
        );
        registry = SimverseAgentRegistry(address(proxy));
        alice = new AgentActor();
        bob = new AgentActor();
    }

    function testCreateAgentPassport() public {
        bytes32 metadataHash = keccak256("agent-metadata-v1");
        uint256 agentId = alice.create(registry, "ipfs://agent-metadata-v1", metadataHash);

        require(agentId == 1, "first agent id should be one");
        require(registry.ownerOf(agentId) == address(alice), "passport owner mismatch");
        require(
            keccak256(bytes(registry.tokenURI(agentId))) == keccak256("ipfs://agent-metadata-v1"),
            "metadata URI mismatch"
        );
        SimverseAgentRegistry.AgentState memory state = registry.agentState(agentId);
        require(state.metadataHash == metadataHash, "metadata digest mismatch");
        require(state.version == 0, "new agent should have no published versions");
    }

    function testOwnerPublishesTrainingVersion() public {
        uint256 agentId = alice.create(registry, "ipfs://agent", keccak256("agent"));
        bytes32 artifactHash = keccak256("weights-and-manifest-v1");
        bytes32 trainingRoot = keccak256("training-set-v1");

        uint64 version = alice.publish(
            registry,
            agentId,
            "ipfs://artifact-v1",
            artifactHash,
            trainingRoot
        );
        SimverseAgentRegistry.AgentState memory state = registry.agentState(agentId);
        require(version == 1 && state.version == 1, "version should advance");
        require(state.latestArtifactHash == artifactHash, "artifact digest mismatch");
        require(state.trainingRoot == trainingRoot, "training root mismatch");
    }

    function testNonOwnerCannotPublish() public {
        uint256 agentId = alice.create(registry, "ipfs://agent", keccak256("agent"));
        bool succeeded;
        try bob.publish(registry, agentId, "ipfs://stolen", keccak256("stolen"), bytes32(0)) {
            succeeded = true;
        } catch {
            succeeded = false;
        }
        require(!succeeded, "non-owner publish should revert");
    }

    function testPassportCannotTransfer() public {
        uint256 agentId = alice.create(registry, "ipfs://agent", keccak256("agent"));
        require(!alice.tryTransfer(registry, address(bob), agentId), "passport must be soulbound");
        require(registry.ownerOf(agentId) == address(alice), "owner changed after failed transfer");
    }

    function testAuthorizedWriterRecordsWorldProof() public {
        uint256 agentId = alice.create(registry, "ipfs://agent", keccak256("agent"));
        bytes32 kind = keccak256("achievement.first_conversation");
        bytes32 dataHash = keccak256("world-proof-payload");
        uint256 proofId = registry.recordWorldProof(agentId, kind, dataHash, 42);

        require(proofId == 0 && registry.worldProofCount(agentId) == 1, "proof count mismatch");
        SimverseAgentRegistry.WorldProof memory proof = registry.worldProof(agentId, proofId);
        require(proof.kind == kind && proof.dataHash == dataHash, "proof payload mismatch");
        require(proof.worldRevision == 42, "world revision mismatch");
    }

    function testOwnerAnchorsAppendOnlyMemoryChain() public {
        uint256 agentId = alice.create(registry, "ipfs://agent", keccak256("agent"));
        bytes32 firstHash = keccak256("memory-snapshot-1");
        bytes32 secondHash = keccak256("memory-snapshot-2");

        require(alice.remember(registry, agentId, "ipfs://memory-1", firstHash) == 1, "bad first revision");
        require(alice.remember(registry, agentId, "ipfs://memory-2", secondHash) == 2, "bad second revision");
        require(registry.memoryAnchorCount(agentId) == 2, "memory count mismatch");

        SimverseAgentRegistry.ContentAnchor memory second = registry.memoryAnchor(agentId, 1);
        require(second.contentHash == secondHash, "memory digest mismatch");
        require(second.parentHash == firstHash, "memory parent mismatch");
        require(second.revision == 2, "memory revision mismatch");
    }

    function testOwnerAnchorsGameSave() public {
        uint256 agentId = alice.create(registry, "ipfs://agent", keccak256("agent"));
        bytes32 saveHash = keccak256("world-save-1");

        require(alice.save(registry, agentId, "ipfs://save-1", saveHash) == 1, "bad save revision");
        SimverseAgentRegistry.AgentState memory state = registry.agentState(agentId);
        require(state.latestSaveHash == saveHash && state.saveRevision == 1, "save state mismatch");
    }

    function testNonOwnerCannotAnchorMemoryOrSave() public {
        uint256 agentId = alice.create(registry, "ipfs://agent", keccak256("agent"));
        bool memorySucceeded;
        bool saveSucceeded;
        try bob.remember(registry, agentId, "ipfs://stolen-memory", keccak256("stolen-memory")) {
            memorySucceeded = true;
        } catch {}
        try bob.save(registry, agentId, "ipfs://stolen-save", keccak256("stolen-save")) {
            saveSucceeded = true;
        } catch {}
        require(!memorySucceeded && !saveSucceeded, "non-owner anchored private state");
    }

    function testUUPSUpgradePreservesAgentState() public {
        uint256 agentId = alice.create(registry, "ipfs://agent", keccak256("agent"));
        SimverseAgentRegistryV2 implementationV2 = new SimverseAgentRegistryV2();
        registry.upgradeToAndCall(address(implementationV2), "");

        SimverseAgentRegistryV2 upgraded = SimverseAgentRegistryV2(address(registry));
        require(upgraded.implementationVersion() == 2, "proxy did not upgrade");
        require(upgraded.ownerOf(agentId) == address(alice), "agent owner was not preserved");
        require(upgraded.nextAgentId() == 2, "registry state was not preserved");
    }

    function testOnlyUpgraderRoleCanUpgrade() public {
        SimverseAgentRegistryV2 implementationV2 = new SimverseAgentRegistryV2();
        require(
            !alice.tryUpgrade(registry, address(implementationV2)),
            "untrusted account upgraded implementation"
        );
    }
}
