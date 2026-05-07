"""
Candidate classifier for current Morpho Blue liquidation opportunities.

Read-only. Scans current candidates, runs production single-hop route checks
where possible, records exact route/simulation failure categories, and ranks by
realizable profit instead of oracle bonus.

Outputs:
  - candidate_classifier.jsonl: one JSON record per candidate
  - candidate_classifier.md: ranked markdown table

No transactions are signed or broadcast.
"""

import argparse
import json
import logging
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
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
    MIN_PROFIT_USD,
    MORPHO_ABI,
    MORPHO_BLUE_ADDRESS,
    SLIPPAGE_BPS,
    STABLE_LOAN_TOKEN_SYMBOLS,
)
from executor import (
    RouteUnavailable,
    build_executor_params,
    estimate_seized_collateral,
    get_executor_contract,
    min_profit_in_loan_token,
    quote_uniswap_single_hop,
)
from scanner import get_token_info, scan_all_markets

EXECUTOR_ERRORS = {
    Web3.keccak(text="NotMorpho()")[:4].hex(): ("NotMorpho", []),
    Web3.keccak(text="UnsupportedFeeTier(uint24)")[:4].hex(): ("UnsupportedFeeTier", ["uint24"]),
    Web3.keccak(text="CollateralNotAllowed(address)")[:4].hex(): ("CollateralNotAllowed", ["address"]),
    Web3.keccak(text="NothingToSwap()")[:4].hex(): ("NothingToSwap", []),
    Web3.keccak(text="InsufficientRepayment(uint256,uint256)")[:4].hex(): (
        "InsufficientRepayment",
        ["uint256", "uint256"],
    ),
    Web3.keccak(text="NotProfitable(uint256,uint256)")[:4].hex(): ("NotProfitable", ["uint256", "uint256"]),
    Web3.keccak(text="OwnableUnauthorizedAccount(address)")[:4].hex(): (
        "OwnableUnauthorizedAccount",
        ["address"],
    ),
}

VIRTUAL_SHARES = 10**6


@dataclass
class CandidateRecord:
    ts: str
    block: int
    category: str
    market_id: str
    borrower: str
    collateral_token: str
    collateral_symbol: str
    loan_token: str
    loan_symbol: str
    is_stale: bool
    ltv: float
    lltv: float
    repaid_shares: str
    estimated_seized_collateral: str
    required_repay: Optional[str]
    oracle_bonus_loan_token: str
    oracle_bonus_usd: Optional[float]
    selected_fee: Optional[int]
    quoted_out: Optional[str]
    amount_out_minimum: Optional[str]
    min_profit: Optional[str]
    min_profit_usd: float
    realizable_profit: Optional[str]
    realizable_profit_usd: Optional[float]
    allowlisted: bool
    simulation_result: Optional[str]
    simulation_revert: Optional[str]
    simulation_profit: Optional[str]


def decode_revert(err: Exception) -> tuple[Optional[str], str, Optional[tuple]]:
    """Decode known executor custom errors."""
    if isinstance(err, ContractCustomError):
        data = err.data if isinstance(err.data, str) else err.data.hex()
        selector = data[2:10] if data.startswith("0x") else data[:8]
        if selector in EXECUTOR_ERRORS:
            name, types = EXECUTOR_ERRORS[selector]
            if not types:
                return name, f"{name}()", ()
            payload = bytes.fromhex(data[10:] if data.startswith("0x") else data[8:])
            try:
                values = abi_decode(types, payload)
                return name, f"{name}({', '.join(str(v) for v in values)})", tuple(values)
            except Exception:
                return name, f"{name}(<undecodable>)", None
        return None, f"UnknownCustomError(selector=0x{selector}, data={data})", None
    return None, f"{type(err).__name__}: {err}", None


def required_repay_assets(morpho: Contract, market_id: bytes, repaid_shares: int) -> int:
    """Compute Morpho borrow assets required for repaid_shares from current market state."""
    market = morpho.functions.market(market_id).call()
    total_borrow_assets = int(market[2])
    total_borrow_shares = int(market[3])
    return repaid_shares * (total_borrow_assets + 1) // (total_borrow_shares + VIRTUAL_SHARES)


def token_price_usd(symbol: str) -> Optional[float]:
    symbol = symbol.upper()
    if symbol in STABLE_LOAN_TOKEN_SYMBOLS:
        return 1.0
    return LOAN_TOKEN_PRICES_USD.get(symbol)


def token_amount_to_usd(amount: int, decimals: int, symbol: str) -> Optional[float]:
    price = token_price_usd(symbol)
    if price is None:
        return None
    return amount / (10**decimals) * price


def choose_best_single_hop_quote(w3: Web3, candidate) -> tuple[Optional[int], Optional[int]]:
    """Return (fee, quote) for the highest-output production single-hop route."""
    amount_in = estimate_seized_collateral(candidate)
    best_fee: Optional[int] = None
    best_quote: Optional[int] = None
    for fee in DEFAULT_UNISWAP_FEE_TIERS:
        quote = quote_uniswap_single_hop(w3, candidate, fee, amount_in)
        if quote is None:
            continue
        if best_quote is None or quote > best_quote:
            best_fee = fee
            best_quote = quote
    return best_fee, best_quote


def simulate_production(executor: Contract, params: tuple) -> tuple[str, Optional[str], Optional[int]]:
    """Run the deployed executor through eth_call with production params."""
    try:
        _, _, profit = executor.functions.executeLiquidation(params).call(
            {"from": Web3.to_checksum_address(BOT_WALLET_ADDRESS)}
        )
        return "ok", None, int(profit)
    except Exception as e:
        name, human, _ = decode_revert(e)
        if name == "NotProfitable":
            return "not_profitable", human, None
        if name == "InsufficientRepayment":
            return "insufficient_repayment", human, None
        if name == "CollateralNotAllowed":
            return "collateral_not_allowed", human, None
        return "contract_revert", human, None


def classify_candidate(
    w3: Web3,
    morpho: Contract,
    executor: Contract,
    candidate,
    block_number: int,
    ts: str,
    allowlist_lower: set[str],
) -> CandidateRecord:
    loan_info = get_token_info(w3, candidate.market_params.loan_token)
    loan_symbol = (candidate.loan_token_symbol or loan_info["symbol"]).upper()
    loan_decimals = int(loan_info["decimals"])
    collateral_symbol = candidate.collateral_token_symbol or "?"
    allowlisted = candidate.market_params.collateral_token.lower() in allowlist_lower

    estimated_seized = estimate_seized_collateral(candidate)
    required_repay: Optional[int] = None
    oracle_bonus_usd = token_amount_to_usd(
        int(candidate.estimated_bonus_in_loan_token),
        loan_decimals,
        loan_symbol,
    )

    selected_fee: Optional[int] = None
    quoted_out: Optional[int] = None
    amount_out_minimum: Optional[int] = None
    min_profit: Optional[int] = None
    realizable_profit: Optional[int] = None
    realizable_profit_usd: Optional[float] = None
    simulation_result: Optional[str] = None
    simulation_revert: Optional[str] = None
    simulation_profit: Optional[int] = None

    if candidate.is_stale:
        category = "stale_or_competed"
    elif token_price_usd(loan_symbol) is None:
        category = "unsupported_asset"
    else:
        try:
            required_repay = required_repay_assets(morpho, candidate.market_id, candidate.borrow_shares)
            min_profit = min_profit_in_loan_token(w3, candidate)
        except RouteUnavailable:
            category = "unsupported_asset"
        except Exception as e:
            category = "route_check_error"
            simulation_revert = f"{type(e).__name__}: {e}"
        else:
            selected_fee, quoted_out = choose_best_single_hop_quote(w3, candidate)
            if selected_fee is None or quoted_out is None:
                category = "no_route"
            else:
                amount_out_minimum = quoted_out * (10_000 - SLIPPAGE_BPS) // 10_000
                realizable_profit = amount_out_minimum - required_repay
                realizable_profit_usd = token_amount_to_usd(
                    realizable_profit,
                    loan_decimals,
                    loan_symbol,
                )

                if amount_out_minimum < required_repay:
                    category = "route_insufficient_repayment"
                elif realizable_profit < min_profit:
                    category = "executable_not_profitable" if allowlisted else "route_repays_below_min_profit"
                elif allowlisted:
                    try:
                        params = build_executor_params(w3, candidate)
                        simulation_result, simulation_revert, simulation_profit = simulate_production(executor, params)
                        category = "executable_profitable" if simulation_result == "ok" else simulation_result
                    except Exception as e:
                        category = "route_check_error"
                        simulation_revert = f"{type(e).__name__}: {e}"
                else:
                    category = "would_be_profitable_but_not_allowlisted"

                if allowlisted and simulation_result is None:
                    try:
                        params = build_executor_params(w3, candidate)
                        simulation_result, simulation_revert, simulation_profit = simulate_production(executor, params)
                    except Exception as e:
                        simulation_result = "route_check_error"
                        simulation_revert = f"{type(e).__name__}: {e}"

    return CandidateRecord(
        ts=ts,
        block=block_number,
        category=category,
        market_id="0x" + candidate.market_id.hex(),
        borrower=candidate.borrower,
        collateral_token=Web3.to_checksum_address(candidate.market_params.collateral_token),
        collateral_symbol=collateral_symbol,
        loan_token=Web3.to_checksum_address(candidate.market_params.loan_token),
        loan_symbol=loan_symbol,
        is_stale=bool(candidate.is_stale),
        ltv=float(candidate.ltv),
        lltv=float(candidate.lltv),
        repaid_shares=str(candidate.borrow_shares),
        estimated_seized_collateral=str(estimated_seized),
        required_repay=str(required_repay) if required_repay is not None else None,
        oracle_bonus_loan_token=str(candidate.estimated_bonus_in_loan_token),
        oracle_bonus_usd=oracle_bonus_usd,
        selected_fee=selected_fee,
        quoted_out=str(quoted_out) if quoted_out is not None else None,
        amount_out_minimum=str(amount_out_minimum) if amount_out_minimum is not None else None,
        min_profit=str(min_profit) if min_profit is not None else None,
        min_profit_usd=float(MIN_PROFIT_USD),
        realizable_profit=str(realizable_profit) if realizable_profit is not None else None,
        realizable_profit_usd=realizable_profit_usd,
        allowlisted=allowlisted,
        simulation_result=simulation_result,
        simulation_revert=simulation_revert,
        simulation_profit=str(simulation_profit) if simulation_profit is not None else None,
    )


def sort_key(record: CandidateRecord) -> tuple:
    profit = record.realizable_profit_usd
    profit_sort = profit if profit is not None else -10**18
    category_rank = {
        "executable_profitable": 0,
        "would_be_profitable_but_not_allowlisted": 1,
        "executable_not_profitable": 2,
        "route_repays_below_min_profit": 3,
        "route_insufficient_repayment": 4,
        "no_route": 5,
        "unsupported_asset": 6,
        "stale_or_competed": 7,
    }.get(record.category, 7)
    return (-profit_sort, category_rank)


ALERT_CATEGORIES = ("executable_profitable", "would_be_profitable_but_not_allowlisted")

# Stable order for the per-cycle category-count summary line.
CATEGORY_ORDER = (
    "executable_profitable",
    "would_be_profitable_but_not_allowlisted",
    "executable_not_profitable",
    "route_repays_below_min_profit",
    "route_insufficient_repayment",
    "no_route",
    "unsupported_asset",
    "stale_or_competed",
    "route_check_error",
)


def write_outputs(
    records: list[CandidateRecord],
    jsonl_path: Path,
    md_path: Path,
    limit: int,
    append: bool = False,
) -> None:
    # JSONL: append builds a historical log; overwrite keeps a snapshot.
    mode = "a" if append else "w"
    with jsonl_path.open(mode, encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(asdict(record), separators=(",", ":")) + "\n")

    # Markdown is always overwritten — it's a snapshot of the latest cycle.
    lines = [
        f"# Candidate classifier report ({records[0].ts if records else 'no records'})",
        "",
        f"Rows: `{len(records)}`. Ranked by realizable profit, then failure category.",
        "",
        "| Rank | Category | Pair | Realizable $ | Oracle bonus $ | Fee | Allowlisted | Sim result | Borrower |",
        "|---:|---|---|---:|---:|---:|:---:|---|---|",
    ]
    for i, record in enumerate(records[:limit], start=1):
        realized = "" if record.realizable_profit_usd is None else f"{record.realizable_profit_usd:.2f}"
        oracle = "" if record.oracle_bonus_usd is None else f"{record.oracle_bonus_usd:.2f}"
        sim = record.simulation_result or record.simulation_revert or ""
        if record.simulation_revert and record.simulation_result:
            sim = f"{record.simulation_result}: {record.simulation_revert}"
        if len(sim) > 80:
            sim = sim[:77] + "..."
        lines.append(
            f"| {i} | {record.category} | {record.collateral_symbol}/{record.loan_symbol} | "
            f"{realized} | {oracle} | {record.selected_fee or '-'} | "
            f"{'Y' if record.allowlisted else 'N'} | {sim} | {record.borrower[:10]}... |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def category_summary(records: list[CandidateRecord]) -> str:
    counts = Counter(r.category for r in records)
    parts = []
    for cat in CATEGORY_ORDER:
        if counts.get(cat):
            parts.append(f"{cat}={counts[cat]}")
    # Anything outside CATEGORY_ORDER (defensive)
    for cat, n in counts.items():
        if cat not in CATEGORY_ORDER:
            parts.append(f"{cat}={n}")
    return " ".join(parts) if parts else "<empty>"


def emit_alerts(records: list[CandidateRecord], log: logging.Logger) -> int:
    """Console/log alerts only. Returns the number of alerts emitted."""
    n = 0
    for r in records:
        if r.category not in ALERT_CATEGORIES:
            continue
        n += 1
        realized = "n/a" if r.realizable_profit_usd is None else f"${r.realizable_profit_usd:.2f}"
        sim_profit = r.simulation_profit if r.simulation_profit is not None else "n/a"
        if r.category == "executable_profitable":
            log.warning(
                f"ALERT executable_profitable | {r.collateral_symbol}/{r.loan_symbol} "
                f"borrower={r.borrower} market={r.market_id} realizable={realized} "
                f"sim_profit={sim_profit} fee={r.selected_fee}"
            )
        else:  # would_be_profitable_but_not_allowlisted
            log.warning(
                f"ALERT would_be_profitable_but_not_allowlisted | "
                f"{r.collateral_symbol}/{r.loan_symbol} ({r.collateral_token}) "
                f"borrower={r.borrower} market={r.market_id} realizable={realized} "
                f"fee={r.selected_fee} -- needs setCollateralAllowed before execution"
            )
    return n


def run_cycle(
    w3: Web3,
    morpho: Contract,
    executor: Contract,
    allowlist_lower: set[str],
    args,
    log: logging.Logger,
) -> int:
    block_number = int(w3.eth.block_number)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.info(f"--- cycle start | block {block_number} ---")

    candidates = scan_all_markets(w3, morpho)
    log.info(f"Scan returned {len(candidates)} candidates")
    if not args.include_stale:
        candidates = [c for c in candidates if not c.is_stale]
        log.info(f"After stale filter: {len(candidates)} candidates")

    records: list[CandidateRecord] = []
    for i, candidate in enumerate(candidates, start=1):
        record = classify_candidate(
            w3=w3, morpho=morpho, executor=executor, candidate=candidate,
            block_number=block_number, ts=ts, allowlist_lower=allowlist_lower,
        )
        records.append(record)
        log.info(
            f"{i}/{len(candidates)} {record.collateral_symbol}/{record.loan_symbol} "
            f"{record.category} realizable=${record.realizable_profit_usd}"
        )

    records.sort(key=sort_key)
    write_outputs(records, Path(args.jsonl), Path(args.md), args.limit, append=args.append)
    log.info(f"Wrote {len(records)} rows to {args.jsonl} ({'append' if args.append else 'overwrite'}) and {args.md}")

    log.info(f"category counts: {category_summary(records)}")
    n_alerts = emit_alerts(records, log)
    if n_alerts == 0:
        log.info("No actionable candidates this cycle")
    return n_alerts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Classify current Morpho liquidation candidates")
    parser.add_argument("--include-stale", action="store_true", help="Include stale/competed candidates")
    parser.add_argument("--jsonl", default="candidate_classifier.jsonl", help="JSONL output path")
    parser.add_argument("--md", default="candidate_classifier.md", help="Markdown output path")
    parser.add_argument("--limit", type=int, default=50, help="Markdown row limit")
    parser.add_argument("--append", action="store_true",
                        help="Append to JSONL instead of overwriting (markdown is always overwritten)")
    parser.add_argument("--interval", type=int, default=30,
                        help="Seconds between cycles when --max-cycles != 1 (default 30)")
    parser.add_argument("--max-cycles", type=int, default=1,
                        help="Stop after N cycles; 0 = unlimited (default 1, single-shot)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("classifier")

    w3 = Web3(Web3.HTTPProvider(ALCHEMY_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("RPC not connected")
        return 1

    log.info(f"Executor: {EXECUTOR_ADDRESS}")
    log.info(f"DRY RUN ONLY: no transactions will be signed or broadcast")
    log.info(
        f"max_cycles={args.max_cycles or 'unlimited'} interval={args.interval}s "
        f"append={'on' if args.append else 'off'}"
    )

    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI,
    )
    executor = get_executor_contract(w3)
    allowlist_lower = {addr.lower() for addr in ALLOWED_COLLATERAL_TOKENS}

    cycle = 0
    try:
        while True:
            cycle += 1
            log.info(f"=== cycle {cycle} ===")
            t0 = time.time()
            try:
                run_cycle(w3, morpho, executor, allowlist_lower, args, log)
            except Exception as e:
                log.error(f"Cycle {cycle} crashed: {type(e).__name__}: {e}")
            elapsed = time.time() - t0
            log.info(f"cycle {cycle} done in {elapsed:.1f}s")

            if args.max_cycles and cycle >= args.max_cycles:
                log.info("Cycle limit reached. Exiting.")
                return 0

            sleep_for = max(0, args.interval - elapsed)
            if sleep_for > 0:
                log.info(f"Sleeping {sleep_for:.1f}s before next cycle")
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        log.info("Interrupted. Exiting.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
