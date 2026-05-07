# Morpho Blue Liquidation Executor Deployment

## Prerequisites

- Foundry installed and available as `forge`
- `ALCHEMY_RPC_URL` set for Ethereum mainnet
- `PRIVATE_KEY` set for the dedicated bot/deployer wallet
- `BOT_WALLET_ADDRESS` set to the owner address

## Install Solidity Dependencies

```powershell
forge install foundry-rs/forge-std
forge install OpenZeppelin/openzeppelin-contracts
```

## Build and Test

```powershell
forge build
forge test --fork-url $env:ALCHEMY_RPC_URL -vvv
```

The real liquidation fork tests are skipped until fixture env vars are set:

```env
FORK_LIQUIDATION_ENABLED=true
FORK_LIQ_LOAN_TOKEN=0x...
FORK_LIQ_COLLATERAL_TOKEN=0x...
FORK_LIQ_ORACLE=0x...
FORK_LIQ_IRM=0x...
FORK_LIQ_LLTV=915000000000000000
FORK_LIQ_BORROWER=0x...
FORK_LIQ_REPAID_SHARES=...
FORK_LIQ_UNISWAP_FEE=3000
FORK_LIQ_AMOUNT_OUT_MINIMUM=...
FORK_LIQ_MIN_PROFIT=50000000
```

Pin the fork block in the command once a fresh candidate is verified:

```powershell
forge test --fork-url $env:ALCHEMY_RPC_URL --fork-block-number <BLOCK> -vvv
```

## Deploy

```powershell
forge script script/DeployExecutor.s.sol:DeployExecutor --rpc-url $env:ALCHEMY_RPC_URL --broadcast
```

After deployment, add the contract address to `.env`:

```env
EXECUTOR_ADDRESS=0x...
```

Allowlist collateral tokens from the owner wallet before live execution:

```solidity
setCollateralAllowed(collateralToken, true)
```

Keep `DRY_RUN=True` until executor simulations and fork tests pass repeatedly.
