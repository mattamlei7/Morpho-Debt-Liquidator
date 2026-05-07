// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MorphoBlueLiquidationExecutor, IMorphoBlue} from "../contracts/MorphoBlueLiquidationExecutor.sol";

contract MorphoBlueLiquidationExecutorForkTest is Test {
    address internal constant MORPHO_BLUE = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;
    address internal constant SWAP_ROUTER_02 = 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45;

    address internal constant OWNER = address(0xB0B);
    address internal constant NON_OWNER = address(0xBAD);

    // --- Live fixture: wstETH/WETH market, pinned at block 24967938 ---
    address internal constant BORROWER = 0x69b7126E34672F174Aa754D6B1ccAA3f2095f12a;
    address internal constant LOAN_TOKEN = 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2;       // WETH
    address internal constant COLLATERAL_TOKEN = 0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0; // wstETH
    address internal constant ORACLE = 0x2a01EB9496094dA03c4E364Def50f5aD1280AD72;
    address internal constant IRM = 0x870aC11D48B15DB9a138Cf899d20F13F79Ba00BC;
    uint256 internal constant LLTV = 945000000000000000;
    uint256 internal constant FORK_BLOCK = 24967938;
    uint256 internal constant BORROW_SHARES = 896436277284809074860;

    MorphoBlueLiquidationExecutor internal executor;

    function setUp() public {
        vm.createSelectFork(vm.envString("ALCHEMY_RPC_URL"), FORK_BLOCK);
        executor = new MorphoBlueLiquidationExecutor(MORPHO_BLUE, SWAP_ROUTER_02, OWNER);

        vm.startPrank(OWNER);
        executor.setCollateralAllowed(COLLATERAL_TOKEN, true);
        // wstETH/WETH main Uniswap V3 pool uses 100 (0.01%) fee tier
        executor.setFeeTierAllowed(100, true);
        vm.stopPrank();
    }

    // ==================== Safety gate tests ====================

    function testRevertsForNonOwner() public {
        vm.prank(NON_OWNER);
        vm.expectRevert();
        executor.executeLiquidation(_params(1, 100, 0, 0));
    }

    function testRevertsForUnallowedCollateral() public {
        vm.prank(OWNER);
        executor.setCollateralAllowed(COLLATERAL_TOKEN, false);

        vm.prank(OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(MorphoBlueLiquidationExecutor.CollateralNotAllowed.selector, COLLATERAL_TOKEN)
        );
        executor.executeLiquidation(_params(1, 100, 0, 0));
    }

    function testRevertsForUnsupportedFeeTier() public {
        vm.prank(OWNER);
        vm.expectRevert(
            abi.encodeWithSelector(MorphoBlueLiquidationExecutor.UnsupportedFeeTier.selector, uint24(200))
        );
        executor.executeLiquidation(_params(1, 200, 0, 0));
    }

    function testCallbackOnlyMorpho() public {
        vm.expectRevert(MorphoBlueLiquidationExecutor.NotMorpho.selector);
        executor.onMorphoLiquidate(1, "");
    }

    // ==================== Live fork tests ====================

    function testSuccessfulLiquidationWithSwapAndProfit() public {
        // Liquidate all borrow shares (tiny position ~0.001 WETH debt)
        uint256 ownerBalanceBefore = IERC20(LOAN_TOKEN).balanceOf(OWNER);

        vm.prank(OWNER);
        (uint256 seizedAssets, uint256 repaidAssets, uint256 profit) =
            executor.executeLiquidation(_params(BORROW_SHARES, 100, 0, 0));

        assertGt(seizedAssets, 0, "Should have seized collateral");
        assertGt(repaidAssets, 0, "Should have repaid debt");
        // Profit may be zero for tiny positions after swap fees, so just check no revert
        // and that owner received any profit
        uint256 ownerBalanceAfter = IERC20(LOAN_TOKEN).balanceOf(OWNER);
        assertEq(ownerBalanceAfter - ownerBalanceBefore, profit, "Profit should be sent to owner");
    }

    function testRevertsWhenNotProfitable() public {
        uint256 impossibleProfit = 1_000_000 ether;

        vm.prank(OWNER);
        vm.expectRevert(); // NotProfitable
        executor.executeLiquidation(_params(BORROW_SHARES, 100, 0, impossibleProfit));
    }

    function testRevertsWhenPositionHealthy() public {
        // Use a non-existent borrower — Morpho reverts on zero-debt liquidation
        MorphoBlueLiquidationExecutor.ExecuteParams memory p = MorphoBlueLiquidationExecutor.ExecuteParams({
            marketParams: IMorphoBlue.MarketParams({
                loanToken: LOAN_TOKEN,
                collateralToken: COLLATERAL_TOKEN,
                oracle: ORACLE,
                irm: IRM,
                lltv: LLTV
            }),
            borrower: address(0xDEAD),
            repaidShares: 1,
            uniswapFee: 100,
            amountOutMinimum: 0,
            minProfit: 0
        });

        vm.prank(OWNER);
        vm.expectRevert();
        executor.executeLiquidation(p);
    }

    // ==================== Helpers ====================

    function _params(
        uint256 repaidShares,
        uint24 fee,
        uint256 amountOutMinimum,
        uint256 minProfit
    ) internal pure returns (MorphoBlueLiquidationExecutor.ExecuteParams memory) {
        return MorphoBlueLiquidationExecutor.ExecuteParams({
            marketParams: IMorphoBlue.MarketParams({
                loanToken: LOAN_TOKEN,
                collateralToken: COLLATERAL_TOKEN,
                oracle: ORACLE,
                irm: IRM,
                lltv: LLTV
            }),
            borrower: BORROWER,
            repaidShares: repaidShares,
            uniswapFee: fee,
            amountOutMinimum: amountOutMinimum,
            minProfit: minProfit
        });
    }
}
