// SPDX-License-Identifier: MIT
pragma solidity ^0.8.34;

import {SimverseAgentRegistry} from "../SimverseAgentRegistry.sol";

contract SimverseAgentRegistryV2 is SimverseAgentRegistry {
    function implementationVersion() external pure returns (uint256) {
        return 2;
    }
}
