// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

interface IMorphoBlue {
    struct MarketParams {
        address loanToken;
        address collateralToken;
        address oracle;
        address irm;
        uint256 lltv;
    }

    function liquidate(
        MarketParams calldata marketParams,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes calldata data
    ) external returns (uint256 seizedAssetsOut, uint256 repaidAssets);
}

interface IMorphoLiquidateCallback {
    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external;
}

interface ISwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params)
        external
        payable
        returns (uint256 amountOut);
}

contract MorphoBlueLiquidationExecutor is IMorphoLiquidateCallback, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    error NotMorpho();
    error UnsupportedFeeTier(uint24 fee);
    error CollateralNotAllowed(address token);
    error NothingToSwap();
    error InsufficientRepayment(uint256 balance, uint256 required);
    error NotProfitable(uint256 finalBalance, uint256 requiredBalance);

    IMorphoBlue public immutable morpho;
    ISwapRouter02 public immutable swapRouter;

    mapping(address token => bool allowed) public allowedCollateral;
    mapping(uint24 fee => bool allowed) public allowedFeeTier;

    struct ExecuteParams {
        IMorphoBlue.MarketParams marketParams;
        address borrower;
        uint256 repaidShares;
        uint24 uniswapFee;
        uint256 amountOutMinimum;
        uint256 minProfit;
    }

    struct CallbackData {
        address loanToken;
        address collateralToken;
        uint24 uniswapFee;
        uint256 amountOutMinimum;
    }

    event CollateralAllowed(address indexed token, bool allowed);
    event FeeTierAllowed(uint24 indexed fee, bool allowed);
    event LiquidationExecuted(
        bytes32 indexed marketId,
        address indexed borrower,
        address indexed loanToken,
        address collateralToken,
        uint256 seizedAssets,
        uint256 repaidAssets,
        uint256 profit
    );
    event Swept(address indexed token, address indexed to, uint256 amount);

    constructor(address morpho_, address swapRouter_, address owner_) Ownable(owner_) {
        morpho = IMorphoBlue(morpho_);
        swapRouter = ISwapRouter02(swapRouter_);

        allowedFeeTier[500] = true;
        allowedFeeTier[3000] = true;
        allowedFeeTier[10000] = true;
    }

    function setCollateralAllowed(address token, bool allowed) external onlyOwner {
        allowedCollateral[token] = allowed;
        emit CollateralAllowed(token, allowed);
    }

    function setFeeTierAllowed(uint24 fee, bool allowed) external onlyOwner {
        allowedFeeTier[fee] = allowed;
        emit FeeTierAllowed(fee, allowed);
    }

    function executeLiquidation(ExecuteParams calldata p)
        external
        onlyOwner
        nonReentrant
        returns (uint256 seizedAssets, uint256 repaidAssets, uint256 profit)
    {
        if (!allowedFeeTier[p.uniswapFee]) revert UnsupportedFeeTier(p.uniswapFee);
        if (!allowedCollateral[p.marketParams.collateralToken]) {
            revert CollateralNotAllowed(p.marketParams.collateralToken);
        }

        IERC20 loanToken = IERC20(p.marketParams.loanToken);
        uint256 startingBalance = loanToken.balanceOf(address(this));

        bytes memory callbackData = abi.encode(
            CallbackData({
                loanToken: p.marketParams.loanToken,
                collateralToken: p.marketParams.collateralToken,
                uniswapFee: p.uniswapFee,
                amountOutMinimum: p.amountOutMinimum
            })
        );

        (seizedAssets, repaidAssets) =
            morpho.liquidate(p.marketParams, p.borrower, 0, p.repaidShares, callbackData);

        uint256 finalBalance = loanToken.balanceOf(address(this));
        uint256 requiredBalance = startingBalance + p.minProfit;
        if (finalBalance < requiredBalance) revert NotProfitable(finalBalance, requiredBalance);

        profit = finalBalance - startingBalance;
        if (profit > 0) loanToken.safeTransfer(owner(), profit);

        emit LiquidationExecuted(
            _marketId(p.marketParams),
            p.borrower,
            p.marketParams.loanToken,
            p.marketParams.collateralToken,
            seizedAssets,
            repaidAssets,
            profit
        );
    }

    function onMorphoLiquidate(uint256 repaidAssets, bytes calldata data) external override {
        if (msg.sender != address(morpho)) revert NotMorpho();

        CallbackData memory decoded = abi.decode(data, (CallbackData));
        IERC20 collateralToken = IERC20(decoded.collateralToken);
        IERC20 loanToken = IERC20(decoded.loanToken);

        uint256 collateralBalance = collateralToken.balanceOf(address(this));
        if (collateralBalance == 0) revert NothingToSwap();

        collateralToken.forceApprove(address(swapRouter), collateralBalance);
        swapRouter.exactInputSingle(
            ISwapRouter02.ExactInputSingleParams({
                tokenIn: decoded.collateralToken,
                tokenOut: decoded.loanToken,
                fee: decoded.uniswapFee,
                recipient: address(this),
                amountIn: collateralBalance,
                amountOutMinimum: decoded.amountOutMinimum,
                sqrtPriceLimitX96: 0
            })
        );
        collateralToken.forceApprove(address(swapRouter), 0);

        uint256 loanBalance = loanToken.balanceOf(address(this));
        if (loanBalance < repaidAssets) revert InsufficientRepayment(loanBalance, repaidAssets);

        loanToken.forceApprove(address(morpho), repaidAssets);
    }

    function sweepToken(address token, address to) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        IERC20(token).safeTransfer(to, balance);
        emit Swept(token, to, balance);
    }

    function _marketId(IMorphoBlue.MarketParams calldata marketParams) internal pure returns (bytes32) {
        return keccak256(abi.encode(marketParams));
    }
}
