# Executor Router Upgrade — Scoping (design only)

**Status:** scoping. No code changes proposed for this iteration. Memo to inform the next contract redesign and redeploy.

**Context:** the v1 executor (`MorphoBlueLiquidationExecutor.sol`, deployed `0x38982bC4268510f76a4243282Cec5ac70f1d2060`) does a single-hop Uniswap V3 swap inside `onMorphoLiquidate`. Step-5 / coverage scout / rsETH probe established that v1 cannot profitably liquidate the high-bonus LRT collateral on Morpho today — the swap leg either has no V3 route or the only deep V3 pool is mispriced relative to the Morpho oracle (rsETH 1% pool: ~0.98 WETH/rsETH vs oracle ~1.067).

This doc scopes a v2 contract that supports route-specific adapters. Do not implement until the open questions below are resolved.

---

## 1. Which LRT pairs need non-V3 routes?

Pulled from the most recent scout cycle. "Bonus $" is oracle-priced; v1 reverts confirm Uniswap-realizable value is much lower.

| Pair (in scan, fresh) | Top oracle bonus $ | V1 single-hop V3 status | Direct Curve pool to WETH? |
|---|---:|---|---|
| hgETH/WETH | 8,714 | no V3 single-hop | **none** (Curve MetaRegistry returned 0 pools) |
| mHyperETH/WETH | 2,826 | no V3 single-hop | **none** |
| MC-USR/USD0 | 374 | no V3 single-hop | (need separate lookup; not USD0-paired in Curve) |
| rsETH/WETH | 113 | quote ok at 1%, **InsufficientRepayment** (pool mispriced) | **none** |
| agETH/WETH | 82 | no V3 single-hop | **none** |
| beraSTONE/WETH | 0.04 | quote at 0.05%, CollateralNotAllowed (allowlist-blocked, depth ~$160k) | **none** |
| wstETH/WETH | 0.03 | OK, profit too small | (not LRT — already executable, just uneconomic) |

**Calibration:** none of the high-bonus pairs have direct Curve pools. A naive Curve adapter would not unlock them.

LRTs that *do* have Curve liquidity but are **not in the current candidate set**: weETH (good — `0xDB74…3dEd5` weETH/WETH-ng pool ~$580k), rswETH (shallow), pufETH (shallow), ezETH (empty). Worth allowlisting opportunistically once Morpho candidate flow includes them, but they don't justify the upgrade on their own today.

**Conclusion for scope:** the design needs to handle (a) Curve adapters for the currently-irrelevant-but-future LRTs, AND (b) some other path for the high-bonus ones. Realistic options for (b):
- **Multi-hop V3** (e.g. `rsETH → wstETH → WETH` if a wstETH leg exists at a fee tier we haven't tested). Cheap to add, same trust model as v1.
- **Pendle PT/SY redemption legs** for LRTs with Pendle markets (rsETH-PT, agETH-PT). Adds Pendle SDK complexity.
- **Aggregator** (1inch / 0x / Cowswap solver) — broadest coverage but biggest trust and calldata-risk surface.

Recommended minimum viable v2: support a small set of explicit adapters (Uniswap V3 single-hop **and** multi-hop, Curve stable/ng pools) before adding any aggregator. Treat aggregators as a separate, later decision once we have evidence multi-hop + Curve aren't enough.

---

## 2. Which exact pools / routers are needed?

For each adapter type, the minimum addresses and on-chain dependencies.

### Uniswap V3 multi-hop (low risk, same router)
- **Router:** `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45` (already in v1 — `SwapRouter02`).
- New function used: `exactInput(ExactInputParams)` instead of `exactInputSingle`.
- Encoded path: bytes (`tokenIn || fee || tokenMid || fee || tokenOut`). Path comes in via `executeLiquidation` calldata, not hardcoded.
- Specific multi-hop probes worth running before designing UI: `rsETH/wstETH/WETH`, `agETH/wstETH/WETH`, `hgETH/wstETH/WETH`. If wstETH leg exists at adequate depth, this is the cheapest unlock.

### Curve adapter
- **Pool router:** Curve does not have a single canonical router for all pool types. Realistic options:
  - **CurveRouter v1.2:** `0x16C6521Dff6baB339122a0FE25a9116693265353` (handles classic + crypto + ng pools; 11 hops max). Path encoding: `address[11] _route, uint256[5][5] _swap_params, uint256 _amount, uint256 _expected, address[5] _pools, address _receiver`.
  - **Per-pool direct call:** safer, simpler, but each new pair requires a code path. Use the pool's own `exchange(int128 i, int128 j, uint256 dx, uint256 min_dy)` (classic) or `exchange(uint256 i, uint256 j, uint256 dx, uint256 min_dy)` (ng / crypto).
- **MetaRegistry:** `0xF98B45FA17DE75FB1aD0e7aFD971b0ca00e379fC` (read-only, used off-chain by the bot to find pools and depth — not called from the executor).

For the LRTs in scope today, direct-pool calls would target zero pools (none exist). Wire CurveRouter only once a candidate (likely weETH/WETH-ng) appears in the candidate set with a real bonus.

### Pendle PT redemption (defer)
- Pendle Router v4: `0x888888888889758F76e7103c6CbF23ABbF58F946`. PT redemption returns SY which then unwraps to underlying.
- Adds Pendle dependency, expiry handling, and a third settlement leg (SY → underlying). Worth scoping separately, not in v2.

### Aggregator (defer)
- 1inch v6 Router: `0x111111125421cA6dc452d289314280a0f8842A65`.
- Risk: the bot would have to trust calldata produced off-chain by the bot operator (or an API). Bad calldata can drain approvals or route through hostile pools. Mitigations available (selector allowlist, target allowlist, pre-execution invariant checks), but complexity is real.

---

## 3. Can the executor support route-specific adapters safely?

Yes, but the safety properties must be preserved invariant by invariant. The v1 contract's safety guarantees are:

1. Only `morpho` can re-enter via `onMorphoLiquidate` (`NotMorpho`).
2. Owner-only entry to `executeLiquidation` (Ownable).
3. Allowlisted collateral and fee tier (`CollateralNotAllowed`, `UnsupportedFeeTier`).
4. Morpho gets repaid in full or revert (`InsufficientRepayment`).
5. Final loan-token balance must beat configured floor (`NotProfitable`).
6. Reentrancy guarded.

A v2 with adapters must keep 1, 2, 4, 5, 6 unchanged and replace 3 with a richer policy:

- **Per-adapter allowlist:** `mapping(address adapter => bool allowed)`. Owner sets which adapter contracts may be invoked from `onMorphoLiquidate`.
- **Per-(collateral,loan,adapter) allowlist:** `mapping(address collateral => mapping(address loan => mapping(address adapter => bool)))`. Prevents a "Curve adapter is allowlisted, so any token can be routed through it" footgun.
- **Approval residue:** explicitly reset allowance to 0 after every adapter swap. v1 already does `forceApprove(router, 0)` after V3 swap; v2 must do the same per-adapter, never leaving non-zero approvals between liquidations.
- **No raw `delegatecall` to adapters.** Adapters live behind a fixed interface and are invoked via `staticcall`/`call`. Adapter contracts hold no state and never custody funds (or are immutable adapters owned by us).
- **No arbitrary calldata pass-through to external routers.** Calldata is built inside the adapter from typed args, not piped through.

Adapter contract pattern (sketch):

```solidity
interface ILiquidationAdapter {
    /// @notice Swap exactly `amountIn` of `tokenIn` to `tokenOut` and send to `recipient`.
    /// Adapter is responsible for approvals; caller pre-funds the adapter with tokenIn.
    /// MUST revert if amountOut < amountOutMin.
    function swap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOutMin,
        bytes calldata routeData
    ) external returns (uint256 amountOut);
}
```

`routeData` is adapter-specific (e.g. for Uniswap V3 multi-hop: the encoded `path` bytes; for Curve: pool address + indices `i,j`).

Two implementations to ship in v2:

- `UniswapV3Adapter` — supports both single-hop (existing) and multi-hop via the same SwapRouter02.
- `CurveAdapter` — supports a small set of pool ABIs (classic, ng, crypto). Uses per-pool calls (no router) to keep the adapter self-contained.

Aggregator adapters are a separate decision; do not include in the v2 ship.

---

## 4. How to encode route data in `executeLiquidation`

Current `ExecuteParams`:

```solidity
struct ExecuteParams {
    IMorphoBlue.MarketParams marketParams;
    address borrower;
    uint256 repaidShares;
    uint24  uniswapFee;            // remove
    uint256 amountOutMinimum;
    uint256 minProfit;
}
```

Proposed v2:

```solidity
struct ExecuteParams {
    IMorphoBlue.MarketParams marketParams;
    address borrower;
    uint256 repaidShares;
    address adapter;               // must be in allowedAdapter
    bytes   routeData;             // adapter-specific encoding
    uint256 amountOutMinimum;
    uint256 minProfit;
}
```

Encoding examples:

- **UniswapV3Adapter, single-hop:** `routeData = abi.encode(uint24 fee)` — adapter calls `exactInputSingle`.
- **UniswapV3Adapter, multi-hop:** `routeData = abi.encodePacked(tokenIn, fee0, tokenMid, fee1, tokenOut)` — adapter calls `exactInput`.
- **CurveAdapter, direct pool:** `routeData = abi.encode(address pool, int128 i, int128 j, bool isNg)` — adapter calls `pool.exchange(i, j, amountIn, amountOutMin)`.

The bot owns building `routeData`. The contract is a dumb executor of the routing decision the bot has already validated off-chain via simulation. This keeps the contract small and simple.

---

## 5. New failure modes (what gets harder)

| Failure mode | Cause | v1 status | v2 mitigation |
|---|---|---|---|
| **Bad route calldata** | Bot encodes wrong path / wrong indices | n/a (single-hop only) | Adapter validates structurally before swap. Sim with `eth_call` is mandatory before broadcast (already required by step-5 gate). |
| **Unknown adapter** | Owner forgets to allowlist a new adapter | n/a | `AdapterNotAllowed(adapter)` revert. Owner-only `setAdapterAllowed`. |
| **Adapter ↔ pair mismatch** | Curve adapter invoked with non-Curve pair | n/a | Per-(collateral,loan,adapter) allowlist gates this at entry. |
| **Approval residue** | Adapter leaves non-zero allowance after swap | partial (v1 already resets V3 router) | Adapter contract enforces post-swap `forceApprove(router, 0)` in its own code; executor does NOT manage allowances on the adapter's behalf. |
| **Reentrancy via adapter** | Hostile or buggy adapter calls back into executor | reentrancy guard catches it | Same guard. Adapters added via owner-only allowlist, not arbitrary. |
| **Sandwich / oracle-Uniswap divergence** | Same as v1 — caller sets `amountOutMinimum`, but a sandwich can push the Uniswap price | reverts as InsufficientRepayment / NotProfitable | Same behavior; no improvement, no regression. Document that liquidations against thin Uniswap pools are inherently MEV-exposed regardless of router. Larger swaps via Curve get less sandwiched than V3 due to LP curve shape, but not zero. |
| **Aggregator solver trust** | If an aggregator adapter is later added, calldata is built off-chain | n/a | Out of scope for v2 ship; revisit with explicit threat model. |
| **Multi-hop fail mid-hop** | First hop succeeds, second fails | n/a | Atomic — swap reverts cleanly, Morpho liquidation reverts cleanly, executor returns to clean state. |
| **Curve i/j swap** | Bot encodes wrong (i,j) — debits wrong coin | n/a | Adapter pre-checks `pool.coins(i) == tokenIn && pool.coins(j) == tokenOut` and reverts. Cheap. |
| **Pool freezes / paused** | Curve admin pauses, V3 pool flags it as inactive | n/a | Swap reverts; Morpho liquidation reverts; executor returns to clean state. No funds at risk beyond gas. |

The principle preserved from v1: **InsufficientRepayment + NotProfitable + reentrancy guard mean the worst case is wasted gas, not principal loss.** v2 must keep this.

---

## 6. Test gates before the next deploy

Required before broadcasting a v2 deploy:

1. Forge unit tests for each adapter (route encoding, allowance reset, revert on mismatch).
2. Forge fork tests for at least one currently-failing real LRT candidate that v1 cannot liquidate. Concretely: pick a fresh rsETH/WETH or weETH/WETH liquidation off mainnet, encode the v2 route, and prove the fork test produces `profit > 0` AND `eth_call gas` is comfortably below `LIQUIDATION_GAS_ESTIMATE`. Without this, redeploy is premature.
3. Repeat the existing v1 wstETH/WETH fork test against v2 to prove no regression on the routes that already work.
4. Re-run `step5_sim.py` and `coverage_scout.py` against the new executor address with `EXECUTOR_ADDRESS` swapped — same pass criteria as v1 (`profit > $50` floor, slippage protection enabled).

Open data still needed before final design:

- Multi-hop V3 probe for `rsETH/wstETH/WETH`, `agETH/wstETH/WETH`, `hgETH/wstETH/WETH` (would be cheapest unlock if any of those legs has depth).
- Liquidity snapshot of weETH/WETH-ng Curve pool over a few hours to confirm its $580k depth is stable, not transient.
- Confirm whether MC-USR/USD0 has a non-Curve, non-V3 venue (Pendle PT? Resolv withdrawal queue?).

---

## 7. Decision request

Three forks for the next step:

- **A.** Run the multi-hop V3 probes first. If any LRT/wstETH/WETH leg has depth, v2 may need only the multi-hop V3 adapter and no Curve work — much smaller scope.
- **B.** Begin implementing the adapter pattern with both `UniswapV3Adapter` (single + multi) and `CurveAdapter`, even though Curve doesn't help today's candidates. Larger upfront scope, more general.
- **C.** Defer v2 entirely. Continue polling on wstETH only and accept no liquidations until either a non-LRT candidate appears or market conditions push the Uniswap rsETH 1% pool back to oracle parity.

Recommendation: **A** first. Cheapest to disprove, and if it works it shrinks v2 to a single-adapter contract.
