// SPDX-License-Identifier: MIT
pragma solidity ^0.8.34;

import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

/// @notice Immutable delay layer for Simverse registry administration/upgrades.
/// @dev The timelock itself is its only administrator. Proposals and execution
/// stay with explicit multisig addresses supplied at deployment.
contract SimverseGovernanceTimelock is TimelockController {
    constructor(
        uint256 minimumDelay,
        address[] memory proposers,
        address[] memory executors
    ) TimelockController(minimumDelay, proposers, executors, address(0)) {}
}
