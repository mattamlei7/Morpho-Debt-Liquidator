"""
Coverage scout: rank Morpho Blue (collateral, loan) pairs by current liquidation
opportunity quality, with route + on-chain simulation evidence. Read-only.

For each (collateral, loan) pair seen in this scan cycle, reports:
  - candidate count and how many are non-stale
  - top estimated bonus in USD
  - aggregate borrow shares
  - per-fee-tier Uniswap V3 single-hop quote success against the highest-bonus candidate
  - selected fee tier and slippage-adjusted amountOutMinimum
  - on-chain eth_call against the deployed executor with minProfit=0
    (CollateralNotAllowed reverts are EXPECTED for any pair not yet allowlisted)

Output: a markdown table to stdout and a JSONL row per pair to coverage_scout.jsonl.
Nothing is broadcast. The on-chain allowlist is unchanged.
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from eth_abi import decode as abi_decode
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractCustomError

from config import (
    ALCHEMY_RPC_URL,
    ALLOWED_COLLATERAL_TOKENS,
    BOT_WALLET_ADDRESS,
    DEFAULT_UNISWAP_FEE_TIERS,
    EXECUTOR_ADDRESS,
    LOAN_TOKEN_PRICES_USD,
    MORPHO_ABI,
    MORPHO_BLUE_ADDRESS,
    SLIPPAGE_BPS,
    STABLE_LOAN_TOKEN_SYMBOLS,
)
from executor import (
    estimate_gas_cost_usd,
    estimate_profit,
    estimate_seized_collateral,
    get_executor_contract,
    quote_uniswap_single_hop,
)
from scanner import scan_all_markets

EXECUTOR_ERRORS = {
    Web3.keccak(text="NotMorpho()")[:4].hex(): ("NotMorpho", []),
    Web3.keccak(text="UnsupportedFeeTier(uint24)")[:4].hex(): ("UnsupportedFeeTier", ["uint24"]),
    Web3.keccak(text="CollateralNotAllowed(address)")[:4].hex(): ("CollateralNotAllowed", ["address"]),
    Web3.keccak(text="NothingToSwap()")[:4].hex(): ("NothingToSwap", []),
    Web3.keccak(text="InsufficientRepayment(uint256,uint256)")[:4].hex(): ("InsufficientRepayment", ["uint256", "uint256"]),
    Web3.keccak(text="NotProfitable(uint256,uint256)")[:4].hex(): ("NotProfitable", ["uint256", "uint256"]),
    Web3.keccak(text="OwnableUnauthorizedAccount(address)")[:4].hex(): ("OwnableUnauthorizedAccount", ["address"]),
}


def decode_revert(err: Exception) -> str:
    if isinstance(err, ContractCustomError):
        data = err.data if isinstance(err.data, str) else err.data.hex()
        sel = data[2:10] if data.startswith("0x") else data[:8]
        if sel in EXECUTOR_ERRORS:
            name, types = EXECUTOR_ERRORS[sel]
            if not types:
                return f"{name}()"
            payload = bytes.fromhex(data[10:] if data.startswith("0x") else data[8:])
            try:
                values = abi_decode(types, payload)
                return f"{name}({', '.join(str(v) for v in values)})"
            except Exception:
                return f"{name}(<undecodable>)"
        return f"UnknownCustomError(0x{sel})"
    return f"{type(err).__name__}: {err}"


def estimate_bonus_usd(candidate, gas_cost_usd: float) -> float:
    info = estimate_profit_safe(candidate, gas_cost_usd)
    return info["bonus_usd_approx"]


def estimate_profit_safe(candidate, gas_cost_usd: float) -> dict:
    sym = (candidate.loan_token_symbol or "").upper()
    if sym in STABLE_LOAN_TOKEN_SYMBOLS:
        bonus = candidate.estimated_bonus_in_loan_token / (10 ** _decimals(candidate))
        return {"bonus_usd_approx": bonus, "net_profit_usd": bonus - gas_cost_usd, "stable": True}
    price = LOAN_TOKEN_PRICES_USD.get(sym)
    if price:
        bonus_tokens = candidate.estimated_bonus_in_loan_token / (10 ** _decimals(candidate))
        return {"bonus_usd_approx": bonus_tokens * price, "net_profit_usd": bonus_tokens * price - gas_cost_usd, "stable": False}
    return {"bonus_usd_approx": float("nan"), "net_profit_usd": float("nan"), "stable": False}


def _decimals(candidate) -> int:
    # cheap heuristic: WETH/wstETH/etc are 18; USDC/USDT are 6.
    sym = (candidate.loan_token_symbol or "").upper()
    if sym in {"USDC", "USDT", "USDCV"}:
        return 6
    return 18


@dataclass
class PairReport:
    collateral: str
    collateral_symbol: str
    loan: str
    loan_symbol: str
    candidate_count: int = 0
    fresh_count: int = 0
    top_bonus_usd: float = 0.0
    sum_borrow_shares: int = 0
    on_chain_allowlisted: bool = False
    loan_priced: bool = False
    representative_borrower: Optional[str] = None
    representative_market_id: Optional[str] = None
    representative_repaid_shares: Optional[int] = None
    quote_results: dict = field(default_factory=dict)  # {fee: int_quote_or_None}
    selected_fee: Optional[int] = None
    amount_out_minimum: Optional[int] = None
    sim_result: Optional[str] = None  # "ok" | revert string
    actual_profit: Optional[int] = None


def quote_all_fee_tiers(w3: Web3, candidate) -> dict[int, Optional[int]]:
    amount_in = estimate_seized_collateral(candidate)
    out: dict[int, Optional[int]] = {}
    for fee in DEFAULT_UNISWAP_FEE_TIERS:
        out[fee] = quote_uniswap_single_hop(w3, candidate, fee, amount_in)
    return out


def simulate_minprofit_zero(
    w3: Web3,
    executor: Contract,
    candidate,
    fee: int,
    amount_out_minimum: int,
) -> tuple[Optional[int], Optional[str]]:
    """Run eth_call with minProfit=0 against deployed executor. Returns (profit, revert_str)."""
    params = (
        candidate.market_params.as_tuple(),
        Web3.to_checksum_address(candidate.borrower),
        candidate.borrow_shares,
        fee,
        amount_out_minimum,
        0,
    )
    try:
        seized, repaid, profit = executor.functions.executeLiquidation(params).call(
            {"from": Web3.to_checksum_address(BOT_WALLET_ADDRESS)}
        )
        return int(profit), None
    except Exception as e:
        return None, decode_revert(e)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Per-pair coverage scout")
    parser.add_argument("--jsonl", type=str, default="coverage_scout.jsonl")
    parser.add_argument("--md", type=str, default="coverage_scout.md")
    parser.add_argument("--include-stale", action="store_true", help="Include stale candidates (default: skip)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("scout")

    w3 = Web3(Web3.HTTPProvider(ALCHEMY_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("RPC not connected")
        return 1

    log.info(f"Executor: {EXECUTOR_ADDRESS}")
    log.info(f"Block:    {w3.eth.block_number}")
    log.info(f"On-chain allowlist (Python mirror): {ALLOWED_COLLATERAL_TOKENS}")

    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI,
    )
    executor = get_executor_contract(w3)

    log.info("Scanning ...")
    candidates = scan_all_markets(w3, morpho)
    log.info(f"Scan returned {len(candidates)} candidates.")

    if not args.include_stale:
        candidates = [c for c in candidates if not c.is_stale]
        log.info(f"After stale filter: {len(candidates)} candidates.")

    if not candidates:
        log.warning("No candidates this cycle. Exiting.")
        return 0

    gas_cost_usd = estimate_gas_cost_usd(w3)
    allowlist_lower = {a.lower() for a in ALLOWED_COLLATERAL_TOKENS}

    # Group by (collateral, loan); pick the highest-bonus representative per pair.
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for c in candidates:
        groups[(c.market_params.collateral_token.lower(), c.market_params.loan_token.lower())].append(c)

    reports: list[PairReport] = []
    for (col_lower, loan_lower), cands in groups.items():
        rep = max(cands, key=lambda c: estimate_profit_safe(c, gas_cost_usd)["bonus_usd_approx"] or 0)
        prof = estimate_profit_safe(rep, gas_cost_usd)
        report = PairReport(
            collateral=rep.market_params.collateral_token,
            collateral_symbol=rep.collateral_token_symbol or "?",
            loan=rep.market_params.loan_token,
            loan_symbol=rep.loan_token_symbol or "?",
            candidate_count=len(cands),
            fresh_count=sum(1 for c in cands if not c.is_stale),
            top_bonus_usd=prof["bonus_usd_approx"] if prof["bonus_usd_approx"] == prof["bonus_usd_approx"] else 0.0,
            sum_borrow_shares=sum(c.borrow_shares for c in cands),
            on_chain_allowlisted=col_lower in allowlist_lower,
            loan_priced=(rep.loan_token_symbol or "").upper() in STABLE_LOAN_TOKEN_SYMBOLS
                        or (rep.loan_token_symbol or "").upper() in LOAN_TOKEN_PRICES_USD,
            representative_borrower=rep.borrower,
            representative_market_id="0x" + rep.market_id.hex(),
            representative_repaid_shares=rep.borrow_shares,
        )

        # Always quote — quote success is independent of allowlist.
        quotes = quote_all_fee_tiers(w3, rep)
        report.quote_results = {str(k): (v if v is None else int(v)) for k, v in quotes.items()}

        best_fee, best_quote = None, None
        for fee, q in quotes.items():
            if q is None:
                continue
            if best_quote is None or q > best_quote:
                best_fee, best_quote = fee, q
        if best_fee is not None:
            report.selected_fee = best_fee
            report.amount_out_minimum = best_quote * (10_000 - SLIPPAGE_BPS) // 10_000

            profit, revert = simulate_minprofit_zero(
                w3, executor, rep, best_fee, report.amount_out_minimum
            )
            if profit is not None:
                report.sim_result = "ok"
                report.actual_profit = profit
            else:
                report.sim_result = revert
        else:
            report.sim_result = "no-uniswap-route"

        reports.append(report)
        log.info(
            f"{report.collateral_symbol}/{report.loan_symbol}: "
            f"n={report.candidate_count} fresh={report.fresh_count} "
            f"top_bonus=${report.top_bonus_usd:.2f} "
            f"allowlisted={report.on_chain_allowlisted} "
            f"sim={report.sim_result}"
        )

    # Rank: prefer (a) loan_priced, (b) top_bonus_usd desc, (c) fresh_count desc.
    reports.sort(key=lambda r: (-(1 if r.loan_priced else 0), -(r.top_bonus_usd or 0), -r.fresh_count))

    # Write JSONL
    jsonl_path = Path(args.jsonl)
    if jsonl_path.exists():
        jsonl_path.unlink()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with jsonl_path.open("a", encoding="utf-8") as f:
        for r in reports:
            row = {
                "ts": ts,
                "block": w3.eth.block_number,
                **{
                    "collateral": r.collateral, "collateral_symbol": r.collateral_symbol,
                    "loan": r.loan, "loan_symbol": r.loan_symbol,
                    "candidate_count": r.candidate_count, "fresh_count": r.fresh_count,
                    "top_bonus_usd": r.top_bonus_usd, "sum_borrow_shares": str(r.sum_borrow_shares),
                    "on_chain_allowlisted": r.on_chain_allowlisted,
                    "loan_priced": r.loan_priced,
                    "representative_borrower": r.representative_borrower,
                    "representative_market_id": r.representative_market_id,
                    "representative_repaid_shares": str(r.representative_repaid_shares) if r.representative_repaid_shares else None,
                    "quote_results": r.quote_results,
                    "selected_fee": r.selected_fee,
                    "amount_out_minimum": str(r.amount_out_minimum) if r.amount_out_minimum else None,
                    "sim_result": r.sim_result,
                    "actual_profit": str(r.actual_profit) if r.actual_profit is not None else None,
                },
            }
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    # Write Markdown table
    md_path = Path(args.md)
    lines = []
    lines.append(f"# Coverage scout report  (block {w3.eth.block_number}, {ts})")
    lines.append("")
    lines.append(f"On-chain allowlist: `{sorted(ALLOWED_COLLATERAL_TOKENS)}`")
    lines.append(f"Stables ($1): `{sorted(STABLE_LOAN_TOKEN_SYMBOLS)}`  Priced non-stables: `{LOAN_TOKEN_PRICES_USD}`")
    lines.append("")
    lines.append("| Pair | n | fresh | top bonus $ | priced? | allowlisted? | fee | sim result | actual profit |")
    lines.append("|---|---:|---:|---:|:---:|:---:|---:|---|---:|")
    for r in reports:
        priced_mark = "[Y]" if r.loan_priced else "[ ]"
        allow_mark = "[Y]" if r.on_chain_allowlisted else "[ ]"
        sim_short = (r.sim_result or "")[:80]
        actual = "" if r.actual_profit is None else f"{r.actual_profit / 10**18:.8f} {r.loan_symbol}"
        lines.append(
            f"| {r.collateral_symbol}/{r.loan_symbol} | {r.candidate_count} | {r.fresh_count} | "
            f"{r.top_bonus_usd:.2f} | {priced_mark} | {allow_mark} | "
            f"{r.selected_fee or '-'} | {sim_short} | {actual} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    log.info(f"Wrote {len(reports)} pair rows to {jsonl_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
