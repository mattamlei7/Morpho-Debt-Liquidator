"""
rsETH/WETH focused probe. Read-only.

Steps:
  1. Confirm rsETH/WETH eligible candidate exists in current scan.
  2. Confirm executor's allowedFeeTier(10000) is true on-chain (no setFeeTierAllowed needed).
  3. Build production ExecuteParams (real minProfit, slippage-protected amountOutMinimum).
  4. eth_call against the deployed executor with a STATE OVERRIDE that flips
     allowedCollateral[rsETH] -> true, simulating the proposed setCollateralAllowed.
     Uses production minProfit. If NotProfitable, re-probes with minProfit=0
     to recover post-swap profit.
  5. eth_estimateGas with the same state override.
  6. rsETH/WETH 0.01%/0.05%/0.30%/1.00% pool inspection: slot0 + liquidity for
     each tier, plus realized slippage vs amountOutMinimum on the chosen pool.
  7. Report. Nothing broadcast. On-chain allowlist unchanged.
"""

import logging
import sys
from typing import Optional

from eth_abi import decode as abi_decode
from eth_utils import to_checksum_address
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractCustomError

from config import (
    ALCHEMY_RPC_URL,
    BOT_WALLET_ADDRESS,
    DEFAULT_UNISWAP_FEE_TIERS,
    ETH_PRICE_USD,
    EXECUTOR_ABI,
    EXECUTOR_ADDRESS,
    LIQUIDATION_GAS_ESTIMATE,
    LOAN_TOKEN_PRICES_USD,
    MIN_PROFIT_USD,
    MORPHO_ABI,
    MORPHO_BLUE_ADDRESS,
    SLIPPAGE_BPS,
    STABLE_LOAN_TOKEN_SYMBOLS,
)
from executor import (
    build_executor_params,
    estimate_gas_cost_usd,
    estimate_seized_collateral,
    quote_uniswap_single_hop,
)
from scanner import scan_all_markets

RSETH = to_checksum_address("0xA1290d69c65A6Fe4DF752f95823fae25cB99e5A7")
WETH = to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
UNISWAP_V3_FACTORY = to_checksum_address("0x1F98431c8aD98523631AE4a59f267346ea31F984")

EXECUTOR_ERRORS = {
    Web3.keccak(text="NotMorpho()")[:4].hex(): ("NotMorpho", []),
    Web3.keccak(text="UnsupportedFeeTier(uint24)")[:4].hex(): ("UnsupportedFeeTier", ["uint24"]),
    Web3.keccak(text="CollateralNotAllowed(address)")[:4].hex(): ("CollateralNotAllowed", ["address"]),
    Web3.keccak(text="NothingToSwap()")[:4].hex(): ("NothingToSwap", []),
    Web3.keccak(text="InsufficientRepayment(uint256,uint256)")[:4].hex(): ("InsufficientRepayment", ["uint256", "uint256"]),
    Web3.keccak(text="NotProfitable(uint256,uint256)")[:4].hex(): ("NotProfitable", ["uint256", "uint256"]),
}

FACTORY_ABI = [{
    "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}],
    "name": "getPool",
    "outputs": [{"type": "address"}],
    "stateMutability": "view",
    "type": "function",
}]

POOL_ABI = [
    {"inputs": [], "name": "slot0",
     "outputs": [{"type": "uint160"}, {"type": "int24"}, {"type": "uint16"}, {"type": "uint16"},
                 {"type": "uint16"}, {"type": "uint8"}, {"type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [{"type": "uint128"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
]


def decode_revert(err: Exception) -> tuple[Optional[str], str]:
    if isinstance(err, ContractCustomError):
        data = err.data if isinstance(err.data, str) else err.data.hex()
        sel = data[2:10] if data.startswith("0x") else data[:8]
        if sel in EXECUTOR_ERRORS:
            name, types = EXECUTOR_ERRORS[sel]
            if not types:
                return name, f"{name}()"
            payload = bytes.fromhex(data[10:] if data.startswith("0x") else data[8:])
            try:
                values = abi_decode(types, payload)
                return name, f"{name}({', '.join(str(v) for v in values)})"
            except Exception:
                return name, f"{name}(<undecodable>)"
        return None, f"UnknownCustomError(0x{sel})"
    return None, f"{type(err).__name__}: {err}"


def storage_slot_for_rseth_allow(w3: Web3) -> str:
    """Slot for allowedCollateral[rsETH] — base slot 1, key=address."""
    key = bytes.fromhex(RSETH[2:].rjust(40, "0").rjust(64, "0"))
    base = (1).to_bytes(32, "big")
    return "0x" + w3.keccak(key + base).hex()


def call_with_override(w3: Web3, executor: Contract, params: tuple, override_slot: str) -> tuple[Optional[tuple], Optional[Exception]]:
    """eth_call against the deployed executor with allowedCollateral[rsETH] forced true."""
    state_override = {
        EXECUTOR_ADDRESS: {
            "stateDiff": {
                override_slot: "0x" + (1).to_bytes(32, "big").hex(),
            }
        }
    }
    try:
        result = executor.functions.executeLiquidation(params).call(
            {"from": to_checksum_address(BOT_WALLET_ADDRESS)},
            state_override=state_override,
        )
        return tuple(result), None
    except Exception as e:
        return None, e


def estimate_gas_with_override(w3: Web3, executor: Contract, params: tuple, override_slot: str) -> tuple[Optional[int], Optional[str]]:
    state_override = {
        EXECUTOR_ADDRESS: {
            "stateDiff": {
                override_slot: "0x" + (1).to_bytes(32, "big").hex(),
            }
        }
    }
    tx = executor.functions.executeLiquidation(params).build_transaction({
        "from": to_checksum_address(BOT_WALLET_ADDRESS),
        "gas": 10_000_000,
    })
    payload = {
        "from": tx["from"],
        "to": tx["to"],
        "data": tx["data"],
    }
    try:
        # web3.py 6.x supports state override on eth_estimateGas via raw RPC
        result = w3.provider.make_request(
            "eth_estimateGas",
            [payload, "latest", state_override],
        )
        if "error" in result:
            return None, result["error"].get("message", str(result["error"]))
        return int(result["result"], 16), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def inspect_pools(w3: Web3, log: logging.Logger) -> dict:
    """Look up rsETH/WETH pools across fee tiers; report slot0, liquidity, token order."""
    factory = w3.eth.contract(address=UNISWAP_V3_FACTORY, abi=FACTORY_ABI)
    out = {}
    for fee in DEFAULT_UNISWAP_FEE_TIERS:
        addr = factory.functions.getPool(RSETH, WETH, fee).call()
        if int(addr, 16) == 0:
            out[fee] = {"pool": None}
            continue
        pool = w3.eth.contract(address=to_checksum_address(addr), abi=POOL_ABI)
        try:
            slot0 = pool.functions.slot0().call()
            liq = pool.functions.liquidity().call()
            t0 = pool.functions.token0().call()
            t1 = pool.functions.token1().call()
            sqrtPriceX96 = slot0[0]
            # price = (sqrtPriceX96 / 2**96)^2 in token1 units per token0 unit (both 18 decimals here)
            price_t1_per_t0 = (sqrtPriceX96 / 2**96) ** 2
            out[fee] = {
                "pool": to_checksum_address(addr),
                "token0": t0, "token1": t1,
                "sqrtPriceX96": sqrtPriceX96,
                "tick": slot0[1],
                "liquidity": liq,
                "price_t1_per_t0": price_t1_per_t0,
            }
        except Exception as e:
            out[fee] = {"pool": addr, "error": f"{type(e).__name__}: {e}"}
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("probe")

    w3 = Web3(Web3.HTTPProvider(ALCHEMY_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("RPC not connected")
        return 1

    log.info(f"Executor: {EXECUTOR_ADDRESS}")
    log.info(f"Block:    {w3.eth.block_number}")
    log.info(f"MIN_PROFIT_USD:    ${MIN_PROFIT_USD}")
    log.info(f"ETH_PRICE_USD:     ${ETH_PRICE_USD} (static)")

    morpho = w3.eth.contract(
        address=to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI,
    )
    executor = w3.eth.contract(
        address=to_checksum_address(EXECUTOR_ADDRESS),
        abi=EXECUTOR_ABI,
    )

    # 1) Confirm fee tier 10000 is allowlisted
    fee_tier_abi = [{"inputs": [{"type": "uint24"}], "name": "allowedFeeTier",
                     "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"}]
    ex_view = w3.eth.contract(address=to_checksum_address(EXECUTOR_ADDRESS), abi=fee_tier_abi)
    fee10000_allowed = ex_view.functions.allowedFeeTier(10000).call()
    log.info(f"On-chain allowedFeeTier(10000): {fee10000_allowed}  -> setFeeTierAllowed needed? {not fee10000_allowed}")

    # 2) Find a fresh rsETH/WETH candidate
    log.info("Scanning for fresh rsETH/WETH candidate ...")
    candidates = scan_all_markets(w3, morpho)
    eligible = [
        c for c in candidates
        if c.market_params.collateral_token.lower() == RSETH.lower()
        and c.market_params.loan_token.lower() == WETH.lower()
        and not c.is_stale
    ]
    log.info(f"Found {len(eligible)} fresh rsETH/WETH candidate(s) of {len(candidates)} total")
    if not eligible:
        log.warning("No fresh rsETH/WETH candidate this cycle. Re-run later.")
        return 2

    gas_cost_usd = estimate_gas_cost_usd(w3)
    target = max(eligible, key=lambda c: c.estimated_bonus_in_loan_token)
    log.info(f"Selected candidate {target.borrower} in market 0x{target.market_id.hex()[:16]}...")

    # 3) Build production params (uses real minProfit)
    try:
        params = build_executor_params(w3, target)
    except Exception as e:
        log.error(f"build_executor_params failed: {e}")
        return 3

    market_params, borrower, repaid_shares, uniswap_fee, amount_out_min, min_profit = params
    log.info(f"selected_uniswap_fee={uniswap_fee}  amount_out_min={amount_out_min}  min_profit={min_profit}")

    # 3b) Pool inspection across all tiers
    log.info("Inspecting rsETH/WETH pools across fee tiers ...")
    pools = inspect_pools(w3, log)

    # Quoted out for the chosen tier (sanity vs amount_out_min)
    amount_in = estimate_seized_collateral(target)
    quoted_out_chosen = quote_uniswap_single_hop(w3, target, uniswap_fee, amount_in)
    realized_slippage_bps = None
    if quoted_out_chosen:
        realized_slippage_bps = ((quoted_out_chosen - amount_out_min) / quoted_out_chosen) * 10_000

    # 4) Probe with state override (allowedCollateral[rsETH] = true)
    override_slot = storage_slot_for_rseth_allow(w3)
    log.info(f"State-override slot for allowedCollateral[rsETH]: {override_slot}")

    log.info("Primary eth_call: production minProfit, with rsETH allowlist override ...")
    primary_result, primary_err = call_with_override(w3, executor, params, override_slot)
    primary_revert_name, primary_revert_str = (None, None)
    if primary_err is not None:
        primary_revert_name, primary_revert_str = decode_revert(primary_err)

    # 4b) If NotProfitable, re-probe with minProfit=0 to recover actual profit
    probe_zero_result = None
    probe_zero_revert = None
    if primary_result is None and primary_revert_name == "NotProfitable":
        zero_params = (market_params, borrower, repaid_shares, uniswap_fee, amount_out_min, 0)
        log.info("Primary reverted NotProfitable; re-probing with minProfit=0 ...")
        probe_zero_result, zero_err = call_with_override(w3, executor, zero_params, override_slot)
        if zero_err is not None:
            _, probe_zero_revert = decode_revert(zero_err)

    # 5) Gas estimate with the same override (use minProfit=0 if production reverts)
    gas_est_params = params if primary_result is not None else (market_params, borrower, repaid_shares, uniswap_fee, amount_out_min, 0)
    gas_est, gas_err = estimate_gas_with_override(w3, executor, gas_est_params, override_slot)

    # ---- Report ----
    print("\n" + "=" * 80)
    print("rsETH/WETH FOCUSED PROBE")
    print("=" * 80)
    print(f"  block                : {w3.eth.block_number}")
    print(f"  rsETH (collateral)   : {RSETH}")
    print(f"  WETH (loan)          : {WETH}")
    print(f"  market_id            : 0x{target.market_id.hex()}")
    print(f"  borrower             : {target.borrower}")
    print(f"  repaid_shares        : {repaid_shares}")
    print(f"  bonus (loan token)   : {target.estimated_bonus_in_loan_token / 10**18:.6f} WETH"
          f" (~ ${target.estimated_bonus_in_loan_token / 10**18 * ETH_PRICE_USD:.2f})")
    print()
    print("  -- Routing --")
    print(f"  selected_uniswap_fee : {uniswap_fee}")
    print(f"  amount_in (seized~)  : {amount_in} ({amount_in / 10**18:.6f} rsETH)")
    print(f"  quoted_out_chosen    : {quoted_out_chosen}  ({(quoted_out_chosen or 0) / 10**18:.6f} WETH)")
    print(f"  amount_out_minimum   : {amount_out_min}  ({amount_out_min / 10**18:.6f} WETH)")
    print(f"  configured slippage  : {SLIPPAGE_BPS} bps")
    if realized_slippage_bps is not None:
        print(f"  realized slip vs quote: {realized_slippage_bps:.2f} bps "
              f"(matches config = {abs(realized_slippage_bps - SLIPPAGE_BPS) < 0.5})")
    print()
    print("  -- Pool depth (rsETH/WETH) --")
    for fee in DEFAULT_UNISWAP_FEE_TIERS:
        info = pools.get(fee, {})
        if not info.get("pool"):
            print(f"  fee={fee:>5}: NO POOL")
            continue
        if "error" in info:
            print(f"  fee={fee:>5}: pool={info['pool']}  error={info['error']}")
            continue
        print(f"  fee={fee:>5}: pool={info['pool']}  liquidity={info['liquidity']}  "
              f"tick={info['tick']}  price(t1/t0)~{info['price_t1_per_t0']:.6f}")
    print()
    print("  -- Simulation (state override: allowedCollateral[rsETH] = true) --")
    print(f"  min_profit (production): {min_profit}  ({min_profit / 10**18:.6f} WETH)")
    if primary_result is not None:
        seized, repaid, profit = primary_result
        print(f"  primary eth_call     : OK")
        print(f"    seizedAssets       : {seized}")
        print(f"    repaidAssets       : {repaid}")
        print(f"    profit             : {profit} ({profit / 10**18:.8f} WETH"
              f" ~ ${profit / 10**18 * ETH_PRICE_USD:.4f})")
        print(f"  verdict              : PASSES PRODUCTION minProfit GATE")
    else:
        print(f"  primary eth_call     : REVERTED  -> {primary_revert_str}")
        if probe_zero_result is not None:
            seized, repaid, profit = probe_zero_result
            actual_usd = profit / 10**18 * ETH_PRICE_USD
            required = min_profit
            print(f"  probe(minProfit=0)   : OK  seized={seized} repaid={repaid} profit={profit}")
            print(f"    actual profit      : {profit / 10**18:.8f} WETH ~ ${actual_usd:.4f}")
            print(f"    required           : {required / 10**18:.8f} WETH ~ ${MIN_PROFIT_USD:.2f}")
            print(f"    shortfall          : {(required - profit) / 10**18:.8f} WETH "
                  f"~ ${(required - profit) / 10**18 * ETH_PRICE_USD:.2f}")
            print(f"  verdict              : EXECUTABLE AT CONTRACT LEVEL, "
                  f"NOT ECONOMICALLY VIABLE for this position")
        elif probe_zero_revert is not None:
            print(f"  probe(minProfit=0)   : REVERTED -> {probe_zero_revert}")
            print(f"  verdict              : CONTRACT-LEVEL FAILURE EVEN AT minProfit=0")
        else:
            print(f"  verdict              : CONTRACT-LEVEL REVERT (not a profit-gate issue)")
    print()
    print("  -- Gas --")
    if gas_est is not None:
        gas_price_gwei = w3.eth.gas_price / 10**9
        gas_eth = gas_est * w3.eth.gas_price / 10**18
        print(f"  eth_estimateGas      : {gas_est} units")
        print(f"  current gas price    : {gas_price_gwei:.2f} gwei")
        print(f"  estimated tx cost    : {gas_eth:.6f} ETH ~ ${gas_eth * ETH_PRICE_USD:.4f}")
        print(f"  config'd LIQUIDATION_GAS_ESTIMATE: {LIQUIDATION_GAS_ESTIMATE}  "
              f"(realistic? {'yes' if gas_est < LIQUIDATION_GAS_ESTIMATE else 'NO — bump'})")
    else:
        print(f"  eth_estimateGas      : failed -> {gas_err}")
    print("=" * 80)
    print()
    print("On-chain allowlist UNCHANGED. No transactions broadcast.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
