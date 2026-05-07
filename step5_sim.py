"""
Step-5 polling harness.

Repeatedly scans for liquidatable positions and simulates each allowlisted
candidate against the deployed executor via eth_call. Uses the production
build_executor_params (including real minProfit). Logs every result — both
NotProfitable negative tests and successful profitable simulations — to a
JSONL file for review.

Stops automatically the first time a simulation passes production minProfit.
Never signs, never broadcasts, never calls execute_liquidation as a transaction.

Usage:
    python step5_sim.py                    # poll forever, 30s interval
    python step5_sim.py --once             # one scan cycle, then exit
    python step5_sim.py --interval 60      # custom interval seconds
    python step5_sim.py --jsonl out.jsonl  # custom output path
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from eth_abi import decode as abi_decode
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractCustomError

from config import (
    ALCHEMY_RPC_URL,
    BOT_WALLET_ADDRESS,
    ETH_PRICE_USD,
    EXECUTOR_ADDRESS,
    LOAN_TOKEN_PRICES_USD,
    MIN_PROFIT_USD,
    MORPHO_ABI,
    MORPHO_BLUE_ADDRESS,
    STABLE_LOAN_TOKEN_SYMBOLS,
)
from executor import (
    RouteUnavailable,
    build_executor_params,
    estimate_gas_cost_usd,
    estimate_profit,
    get_executor_contract,
)
from scanner import scan_all_markets

WSTETH = "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"

EXECUTOR_ERRORS = {
    Web3.keccak(text="NotMorpho()")[:4].hex(): ("NotMorpho", []),
    Web3.keccak(text="UnsupportedFeeTier(uint24)")[:4].hex(): ("UnsupportedFeeTier", ["uint24"]),
    Web3.keccak(text="CollateralNotAllowed(address)")[:4].hex(): ("CollateralNotAllowed", ["address"]),
    Web3.keccak(text="NothingToSwap()")[:4].hex(): ("NothingToSwap", []),
    Web3.keccak(text="InsufficientRepayment(uint256,uint256)")[:4].hex(): ("InsufficientRepayment", ["uint256", "uint256"]),
    Web3.keccak(text="NotProfitable(uint256,uint256)")[:4].hex(): ("NotProfitable", ["uint256", "uint256"]),
    Web3.keccak(text="OwnableUnauthorizedAccount(address)")[:4].hex(): ("OwnableUnauthorizedAccount", ["address"]),
}


def decode_revert(err: Exception) -> tuple[Optional[str], str, Optional[tuple]]:
    """Decode a contract revert. Returns (error_name, human_string, decoded_values)."""
    if isinstance(err, ContractCustomError):
        data = err.data if isinstance(err.data, str) else err.data.hex()
        sel = data[2:10] if data.startswith("0x") else data[:8]
        if sel in EXECUTOR_ERRORS:
            name, types = EXECUTOR_ERRORS[sel]
            if not types:
                return name, f"{name}()", ()
            payload = bytes.fromhex(data[10:] if data.startswith("0x") else data[8:])
            try:
                values = abi_decode(types, payload)
                return name, f"{name}({', '.join(str(v) for v in values)})", tuple(values)
            except Exception:
                return name, f"{name}(<undecodable: {data}>)", None
        return None, f"UnknownCustomError(selector=0x{sel}, data={data})", None
    return None, f"{type(err).__name__}: {err}", None


def call_executor(executor: Contract, params: tuple, from_addr: str):
    return executor.functions.executeLiquidation(params).call({
        "from": Web3.to_checksum_address(from_addr),
    })


def simulate_one(
    w3: Web3,
    executor: Contract,
    candidate,
    log: logging.Logger,
) -> dict:
    """Simulate a single candidate via production routing. Returns a record dict."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "block": w3.eth.block_number,
        "market_id": "0x" + candidate.market_id.hex(),
        "loan_token": candidate.market_params.loan_token,
        "loan_token_symbol": candidate.loan_token_symbol,
        "collateral_token": candidate.market_params.collateral_token,
        "collateral_token_symbol": candidate.collateral_token_symbol,
        "borrower": candidate.borrower,
        "repaid_shares": str(candidate.borrow_shares),
        "uniswap_fee": None,
        "amount_out_minimum": None,
        "min_profit": None,
        "result": None,            # "ok" | "not_profitable" | "contract_revert" | "route_unavailable" | "error"
        "actual_profit": None,     # str (wei) when known
        "required_profit": None,   # str (wei) when known
        "seized_assets": None,
        "repaid_assets": None,
        "revert": None,
    }

    try:
        params = build_executor_params(w3, candidate)
    except RouteUnavailable as e:
        record["result"] = "route_unavailable"
        record["revert"] = str(e)
        return record
    except Exception as e:
        record["result"] = "error"
        record["revert"] = f"{type(e).__name__}: {e}"
        return record

    _, _, repaid_shares, uniswap_fee, amount_out_min, min_profit = params
    record["uniswap_fee"] = uniswap_fee
    record["amount_out_minimum"] = str(amount_out_min)
    record["min_profit"] = str(min_profit)

    try:
        seized, repaid, profit = call_executor(executor, params, BOT_WALLET_ADDRESS)
        record["result"] = "ok"
        record["seized_assets"] = str(seized)
        record["repaid_assets"] = str(repaid)
        record["actual_profit"] = str(profit)
        return record
    except Exception as e:
        name, human, values = decode_revert(e)
        record["revert"] = human
        if name == "NotProfitable" and values is not None and len(values) == 2:
            actual, required = values
            record["result"] = "not_profitable"
            record["actual_profit"] = str(actual)
            record["required_profit"] = str(required)
        else:
            record["result"] = "contract_revert"
        return record


def write_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def summarize_record_for_log(rec: dict) -> str:
    base = (
        f"block={rec['block']} "
        f"{rec['collateral_token_symbol']}/{rec['loan_token_symbol']} "
        f"borrower={rec['borrower'][:10]}... "
        f"shares={rec['repaid_shares']}"
    )
    if rec["result"] == "ok":
        return (
            f"OK | {base} | seized={rec['seized_assets']} repaid={rec['repaid_assets']} "
            f"profit={rec['actual_profit']}"
        )
    if rec["result"] == "not_profitable":
        return (
            f"NOT_PROFITABLE | {base} | actual={rec['actual_profit']} "
            f"required={rec['required_profit']}"
        )
    if rec["result"] == "route_unavailable":
        return f"ROUTE_UNAVAILABLE | {base} | {rec['revert']}"
    return f"{(rec['result'] or 'unknown').upper()} | {base} | {rec['revert']}"


def run_cycle(
    w3: Web3,
    morpho: Contract,
    executor: Contract,
    jsonl_path: Path,
    log: logging.Logger,
) -> tuple[int, int]:
    """Run one scan + simulate cycle. Returns (n_simulated, n_passed)."""
    log.info("--- scan cycle starting ---")
    candidates = scan_all_markets(w3, morpho)
    if not candidates:
        log.info("No candidates this cycle.")
        return 0, 0

    eligible = [
        c for c in candidates
        if c.market_params.collateral_token.lower() == WSTETH.lower()
        and not c.is_stale
    ]
    log.info(
        f"Scan: {len(candidates)} total, {len(eligible)} are wstETH (allowlisted) and not stale."
    )
    if not eligible:
        return 0, 0

    gas_cost_usd = estimate_gas_cost_usd(w3)
    eligible.sort(
        key=lambda c: estimate_profit(w3, c, gas_cost_usd)["net_profit_usd"],
        reverse=True,
    )

    n_simulated = 0
    n_passed = 0
    for cand in eligible:
        rec = simulate_one(w3, executor, cand, log)
        write_jsonl(jsonl_path, rec)
        log.info(summarize_record_for_log(rec))
        n_simulated += 1
        if rec["result"] == "ok":
            n_passed += 1
            return n_simulated, n_passed  # stop scanning further candidates this cycle
    return n_simulated, n_passed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Step-5 polling simulation harness")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between cycles (default 30)")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--max-cycles", type=int, default=0, help="Stop after N cycles (0 = unlimited)")
    parser.add_argument("--jsonl", type=str, default="step5_sim.jsonl", help="Output JSONL path")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("step5")

    w3 = Web3(Web3.HTTPProvider(ALCHEMY_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("RPC not connected")
        return 1

    log.info(f"Executor:           {EXECUTOR_ADDRESS}")
    log.info(f"Bot wallet:         {BOT_WALLET_ADDRESS}")
    log.info(f"Chain {w3.eth.chain_id} block {w3.eth.block_number}")
    log.info(f"MIN_PROFIT_USD:     ${MIN_PROFIT_USD}")
    log.info(f"ETH_PRICE_USD:      ${ETH_PRICE_USD} (static)")
    log.info(f"Stables (==$1):     {sorted(STABLE_LOAN_TOKEN_SYMBOLS)}")
    log.info(f"Priced non-stables: {LOAN_TOKEN_PRICES_USD}")
    log.info(f"JSONL output:       {args.jsonl}")

    jsonl_path = Path(args.jsonl)
    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI,
    )
    executor = get_executor_contract(w3)

    cycle = 0
    total_sim = 0
    total_pass = 0
    try:
        while True:
            cycle += 1
            log.info(f"=== cycle {cycle} ===")
            t0 = time.time()
            try:
                n_sim, n_pass = run_cycle(w3, morpho, executor, jsonl_path, log)
            except Exception as e:
                log.error(f"Cycle {cycle} crashed: {type(e).__name__}: {e}")
                n_sim, n_pass = 0, 0
            total_sim += n_sim
            total_pass += n_pass
            elapsed = time.time() - t0
            log.info(f"cycle {cycle} done in {elapsed:.1f}s; total_sim={total_sim} total_pass={total_pass}")

            if total_pass > 0:
                log.info(
                    "PASS condition met: at least one candidate cleared production minProfit. "
                    "Stopping. Review JSONL and confirm the production scan would naturally "
                    "select this same candidate before discussing live mode."
                )
                return 0

            if args.once or (args.max_cycles and cycle >= args.max_cycles):
                log.info("Cycle limit reached without a passing candidate. Exiting.")
                return 0

            sleep_for = max(0, args.interval - elapsed)
            if sleep_for > 0:
                log.info(f"Sleeping {sleep_for:.1f}s before next cycle.")
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        log.info("Interrupted. Exiting.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
