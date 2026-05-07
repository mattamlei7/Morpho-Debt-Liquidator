"""
Multi-hop Uniswap V3 probe (LRT -> wstETH -> WETH). Read-only.

For each LRT collateral seen in the current scout candidate set:
  1. Find the fresh Morpho candidate (highest borrow shares, not stale).
  2. Compute amountIn from estimate_seized_collateral (candidate-sized).
  3. Verify a V3 pool exists for LRT/wstETH at each fee tier.
  4. Run QuoterV2.quoteExactInput for path
       LRT --(fee_a)--> wstETH --(100)--> WETH
     across fee_a in {100, 500, 3000, 10000} (skipping non-existent pools).
  5. Compare quoted WETH out against:
       - Morpho `expectedRepay` (debt amount in WETH for repaidShares)
       - production minProfit (price-aware via LOAN_TOKEN_PRICES_USD)
       - oracle-priced bonus (estimated_bonus_in_loan_token)
  6. Report. No state override, no on-chain action.

Pass condition for v2 scope: at least one currently-failing high-bonus
candidate has a multi-hop V3 route that repays Morpho AND clears production
minProfit after slippage.
"""

import logging
import sys
from dataclasses import dataclass
from typing import Optional

from eth_abi.packed import encode_packed
from eth_utils import to_checksum_address
from web3 import Web3

from config import (
    ALCHEMY_RPC_URL,
    DEFAULT_UNISWAP_FEE_TIERS,
    ETH_PRICE_USD,
    LOAN_TOKEN_PRICES_USD,
    MIN_PROFIT_USD,
    MORPHO_ABI,
    MORPHO_BLUE_ADDRESS,
    SLIPPAGE_BPS,
    UNISWAP_QUOTER_V2_ADDRESS,
    WAD,
)
from executor import estimate_seized_collateral
from scanner import scan_all_markets

WSTETH = to_checksum_address("0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0")
WETH = to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
UNISWAP_V3_FACTORY = to_checksum_address("0x1F98431c8aD98523631AE4a59f267346ea31F984")
WSTETH_WETH_LEG_FEE = 100  # the deep, oracle-aligned leg

LRTS = {
    "rsETH":     "0xA1290d69c65A6Fe4DF752f95823fae25cB99e5A7",
    "agETH":     "0xe1B4d34E8754600962Cd944B535180Bd758E6c2e",
    "hgETH":     "0xc824A08dB624942c5E5F330d56530cD1598859fD",
    "mHyperETH": "0x5a42864b14C0C8241EF5ab62Dae975b163a2E0C1",
    "beraSTONE": "0x97Ad75064b20fb2B2447feD4fa953bF7F007a706",
}

FACTORY_ABI = [{
    "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}],
    "name": "getPool", "outputs": [{"type": "address"}],
    "stateMutability": "view", "type": "function",
}]

POOL_ABI = [
    {"inputs": [], "name": "liquidity", "outputs": [{"type": "uint128"}],
     "stateMutability": "view", "type": "function"},
]

QUOTER_EXACT_INPUT_ABI = [{
    "inputs": [{"type": "bytes"}, {"type": "uint256"}],
    "name": "quoteExactInput",
    "outputs": [
        {"type": "uint256"},
        {"type": "uint160[]"},
        {"type": "uint32[]"},
        {"type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
}]


@dataclass
class HopProbe:
    fee_lrt_leg: int
    pool_lrt_wsteth: Optional[str]
    pool_wsteth_weth: Optional[str]
    quoted_out_weth: Optional[int]
    error: Optional[str] = None


@dataclass
class CandidateProbe:
    lrt_symbol: str
    lrt_address: str
    has_fresh_candidate: bool
    market_id: Optional[str] = None
    borrower: Optional[str] = None
    repaid_shares: Optional[int] = None
    amount_in_lrt: Optional[int] = None
    expected_repay_weth: Optional[int] = None
    oracle_bonus_weth: Optional[int] = None
    min_profit_weth: Optional[int] = None
    hop_results: list[HopProbe] = None
    best_hop: Optional[HopProbe] = None


def required_repay_weth(w3: Web3, morpho, market_id: bytes, repaid_shares: int) -> int:
    """Compute the WETH amount Morpho will require to repay `repaid_shares`. Uses on-chain
    market state: repaidAssets = repaidShares * (totalBorrowAssets+1) / (totalBorrowShares+1e6).
    """
    market = morpho.functions.market(market_id).call()
    total_borrow_assets = market[2]
    total_borrow_shares = market[3]
    VIRTUAL_SHARES = 10**6
    return (repaid_shares * (total_borrow_assets + 1)) // (total_borrow_shares + VIRTUAL_SHARES)


def encode_path(token_in: str, fee_a: int, token_mid: str, fee_b: int, token_out: str) -> bytes:
    return encode_packed(
        ["address", "uint24", "address", "uint24", "address"],
        [to_checksum_address(token_in), fee_a, to_checksum_address(token_mid), fee_b, to_checksum_address(token_out)],
    )


def get_pool(factory, a: str, b: str, fee: int) -> Optional[str]:
    p = factory.functions.getPool(to_checksum_address(a), to_checksum_address(b), fee).call()
    return None if int(p, 16) == 0 else to_checksum_address(p)


def quote_multihop(w3: Web3, quoter, path_bytes: bytes, amount_in: int) -> tuple[Optional[int], Optional[str]]:
    try:
        amount_out, _, _, _ = quoter.functions.quoteExactInput(path_bytes, amount_in).call()
        return int(amount_out), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("multihop")

    w3 = Web3(Web3.HTTPProvider(ALCHEMY_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("RPC not connected")
        return 1

    log.info(f"Block: {w3.eth.block_number}")
    log.info(f"MIN_PROFIT_USD: ${MIN_PROFIT_USD}; ETH_PRICE_USD (static): ${ETH_PRICE_USD}")
    log.info(f"Mid-leg pinned to wstETH/WETH fee={WSTETH_WETH_LEG_FEE}")

    morpho = w3.eth.contract(
        address=to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI,
    )
    factory = w3.eth.contract(address=UNISWAP_V3_FACTORY, abi=FACTORY_ABI)
    quoter = w3.eth.contract(
        address=to_checksum_address(UNISWAP_QUOTER_V2_ADDRESS),
        abi=QUOTER_EXACT_INPUT_ABI,
    )

    # Confirm wstETH/WETH leg exists
    leg_pool = get_pool(factory, WSTETH, WETH, WSTETH_WETH_LEG_FEE)
    log.info(f"wstETH/WETH leg fee={WSTETH_WETH_LEG_FEE} pool={leg_pool}")
    if leg_pool is None:
        log.error("wstETH/WETH leg pool missing — cannot multi-hop")
        return 2

    log.info("Scanning for fresh candidates ...")
    candidates = scan_all_markets(w3, morpho)
    fresh = [c for c in candidates if not c.is_stale]
    log.info(f"Total {len(candidates)} candidates, {len(fresh)} fresh (non-stale)")

    # Pre-compute production min_profit in WETH using LOAN_TOKEN_PRICES_USD
    weth_price = LOAN_TOKEN_PRICES_USD.get("WETH")
    if not weth_price:
        log.error("WETH price missing from LOAN_TOKEN_PRICES_USD")
        return 3
    min_profit_weth = int(MIN_PROFIT_USD / weth_price * WAD)
    log.info(f"Production minProfit @ ETH=${weth_price}: {min_profit_weth} wei (~{min_profit_weth/WAD:.6f} WETH)")

    probes: list[CandidateProbe] = []
    for sym, addr in LRTS.items():
        addr_c = to_checksum_address(addr)
        # Find the fresh candidate with largest borrow shares for this LRT against WETH
        match = [
            c for c in fresh
            if c.market_params.collateral_token.lower() == addr.lower()
            and c.market_params.loan_token.lower() == WETH.lower()
        ]
        if not match:
            probes.append(CandidateProbe(lrt_symbol=sym, lrt_address=addr_c, has_fresh_candidate=False, hop_results=[]))
            continue

        target = max(match, key=lambda c: c.borrow_shares)
        amount_in = estimate_seized_collateral(target)
        repay_weth = required_repay_weth(w3, morpho, target.market_id, target.borrow_shares)
        oracle_bonus = target.estimated_bonus_in_loan_token

        hops: list[HopProbe] = []
        for fee_a in DEFAULT_UNISWAP_FEE_TIERS:
            pool_lrt_wsteth = get_pool(factory, addr_c, WSTETH, fee_a)
            if pool_lrt_wsteth is None:
                hops.append(HopProbe(fee_lrt_leg=fee_a, pool_lrt_wsteth=None,
                                     pool_wsteth_weth=leg_pool, quoted_out_weth=None,
                                     error="no LRT/wstETH pool"))
                continue
            path = encode_path(addr_c, fee_a, WSTETH, WSTETH_WETH_LEG_FEE, WETH)
            quoted, err = quote_multihop(w3, quoter, path, amount_in)
            hops.append(HopProbe(
                fee_lrt_leg=fee_a,
                pool_lrt_wsteth=pool_lrt_wsteth,
                pool_wsteth_weth=leg_pool,
                quoted_out_weth=quoted,
                error=err,
            ))

        successful = [h for h in hops if h.quoted_out_weth is not None]
        best = max(successful, key=lambda h: h.quoted_out_weth) if successful else None

        probes.append(CandidateProbe(
            lrt_symbol=sym,
            lrt_address=addr_c,
            has_fresh_candidate=True,
            market_id="0x" + target.market_id.hex(),
            borrower=target.borrower,
            repaid_shares=target.borrow_shares,
            amount_in_lrt=amount_in,
            expected_repay_weth=repay_weth,
            oracle_bonus_weth=oracle_bonus,
            min_profit_weth=min_profit_weth,
            hop_results=hops,
            best_hop=best,
        ))

    # ---- Report ----
    print("\n" + "=" * 90)
    print("MULTI-HOP V3 PROBE  (path: LRT --(fee_a)--> wstETH --(100)--> WETH)")
    print("=" * 90)
    any_pass = False
    for p in probes:
        print()
        print(f"## {p.lrt_symbol} ({p.lrt_address})")
        if not p.has_fresh_candidate:
            print("  no fresh /WETH candidate this cycle")
            continue
        print(f"  market_id          : {p.market_id}")
        print(f"  borrower           : {p.borrower}")
        print(f"  repaid_shares      : {p.repaid_shares}")
        print(f"  amountIn (rsETH-equivalent collateral): {p.amount_in_lrt} ({p.amount_in_lrt/WAD:.6f} {p.lrt_symbol})")
        print(f"  required repay     : {p.expected_repay_weth} ({p.expected_repay_weth/WAD:.6f} WETH)")
        print(f"  oracle bonus       : {p.oracle_bonus_weth} ({p.oracle_bonus_weth/WAD:.6f} WETH ~ ${p.oracle_bonus_weth/WAD*ETH_PRICE_USD:.2f})")
        print(f"  production minProfit: {p.min_profit_weth} ({p.min_profit_weth/WAD:.6f} WETH ~ $50)")
        print()
        print(f"  {'fee_a':>6} | {'pool LRT/wstETH':<42} | {'quoted WETH out':>22} | {'vs repay':>14} | verdict")
        for h in p.hop_results:
            pool_str = h.pool_lrt_wsteth or "<none>"
            if h.quoted_out_weth is None:
                qstr = "—"
                cmp_str = "—"
                verdict = h.error or "no quote"
            else:
                qstr = f"{h.quoted_out_weth} ({h.quoted_out_weth/WAD:.6f})"
                surplus = h.quoted_out_weth - p.expected_repay_weth
                cmp_str = f"{surplus/WAD:+.6f}"
                # Apply slippage to be honest about realized profit
                amount_out_min = h.quoted_out_weth * (10_000 - SLIPPAGE_BPS) // 10_000
                final_after_slippage = amount_out_min - p.expected_repay_weth
                if final_after_slippage >= p.min_profit_weth:
                    verdict = f"PASS ({final_after_slippage/WAD:.6f} WETH net after slippage)"
                    any_pass = True
                elif amount_out_min >= p.expected_repay_weth:
                    verdict = f"REPAYS but below minProfit (net {final_after_slippage/WAD:+.6f} WETH after slip)"
                else:
                    verdict = f"INSUFFICIENT REPAY (short {(p.expected_repay_weth - amount_out_min)/WAD:.6f} WETH after slip)"
            print(f"  {h.fee_lrt_leg:>6} | {pool_str:<42} | {qstr:>22} | {cmp_str:>14} | {verdict}")

    print()
    print("=" * 90)
    print(f"PASS CONDITION (v2 scope unlock): {'YES — at least one route passes' if any_pass else 'NO — no multi-hop path repays + clears minProfit'}")
    print("=" * 90)
    return 0 if any_pass else 4


if __name__ == "__main__":
    sys.exit(main())
