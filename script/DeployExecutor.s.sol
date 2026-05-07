// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {MorphoBlueLiquidationExecutor} from "../contracts/MorphoBlueLiquidationExecutor.sol";

contract DeployExecutor is Script {
    address internal constant MORPHO_BLUE = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address internal constant SWAP_ROUTER_02 = 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45;

    // Collateral tokens to allowlist on deploy
    address internal constant WSTETH = 0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0;

    function run() external returns (MorphoBlueLiquidationExecutor executor) {
        uint256 privateKey = vm.envUint("PRIVATE_KEY");
        address owner = vm.envAddress("BOT_WALLET_ADDRESS");

        vm.startBroadcast(privateKey);

        executor = new MorphoBlueLiquidationExecutor(MORPHO_BLUE, SWAP_ROUTER_02, owner);

        // Allowlist wstETH collateral
        executor.setCollateralAllowed(WSTETH, true);

        // Add 100 bps (0.01%) fee tier for correlated-asset pools (wstETH/WETH)
        executor.setFeeTierAllowed(100, true);

        vm.stopBroadcast();

        console.log("Executor deployed at:", address(executor));
        console.log("Owner:", owner);
        console.log("wstETH allowlisted:", executor.allowedCollateral(WSTETH));
        console.log("Fee tier 100 allowed:", executor.allowedFeeTier(100));
    }
}
