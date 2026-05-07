"""
Executor module for Morpho Blue Liquidation Bot.
Profitability analysis, Uniswap route quoting, executor simulation, and execution.
"""

import logging
from typing import Optional

from web3 import Web3
from web3.contract import Contract

from config import (
    ALLOWED_COLLATERAL_TOKENS,
    BOT_WALLET_ADDRESS,
    COMPETITION_CHECK_TOP_N,
    DEFAULT_UNISWAP_FEE_TIERS,
    DRY_RUN,
    ETH_PRICE_USD,
    EXECUTOR_ABI,
    EXECUTOR_ADDRESS,
    LIQUIDATION_GAS_ESTIMATE,
    LOAN_TOKEN_PRICES_USD,
    MAX_GAS_PRICE_GWEI,
    MIN_PROFIT_USD,
    PRIVATE_KEY,
    QUOTER_V2_ABI,
    SLIPPAGE_BPS,
    STABLE_LOAN_TOKEN_SYMBOLS,
    UNISWAP_QUOTER_V2_ADDRESS,
)
from competition import HIGH, check_competition_batch
from scanner import LiquidationCandidate, get_token_info

logger = logging.getLogger(__name__)

# Fallback address used as the "from" in dry-run simulations when no bot wallet is configured.
SIMULATION_FROM = "0x0000000000000000000000000000000000000001"


class RouteUnavailable(Exception):
    """Raised when no safe single-hop Uniswap V3 route can be quoted."""


def estimate_gas_cost_usd(w3: Web3) -> float:
    """Estimate gas cost for a liquidation transaction in USD."""
    try:
        gas_price = w3.eth.gas_price
    except Exception:
        gas_price = Web3.to_wei(30, "gwei")

    gas_cost_eth = gas_price * LIQUIDATION_GAS_ESTIMATE / 10**18
    return gas_cost_eth * ETH_PRICE_USD


def current_gas_price_gwei(w3: Web3) -> float:
    """Return current gas price in gwei."""
    return w3.eth.gas_price / 10**9


def gas_price_is_acceptable(w3: Web3) -> bool:
    """Check configured gas ceiling before simulation or execution."""
    gas_price_gwei = current_gas_price_gwei(w3)
    if gas_price_gwei > MAX_GAS_PRICE_GWEI:
        logger.warning(
            f"Gas price too high: {gas_price_gwei:.1f} gwei > "
            f"{MAX_GAS_PRICE_GWEI} gwei. Skipping execution."
        )
        return False
    return True


def get_bot_account(w3: Web3):
    """Load the executor account from PRIVATE_KEY in .env."""
    if not PRIVATE_KEY:
        raise EnvironmentError("PRIVATE_KEY is required when DRY_RUN is False")

    account = w3.eth.account.from_key(PRIVATE_KEY)
    derived_address = Web3.to_checksum_address(account.address)

    if BOT_WALLET_ADDRESS:
        configured_address = Web3.to_checksum_address(BOT_WALLET_ADDRESS)
        if configured_address != derived_address:
            raise EnvironmentError(
                "BOT_WALLET_ADDRESS does not match the address derived from PRIVATE_KEY"
            )

    return account


def get_executor_contract(w3: Web3) -> Contract:
    """Return the deployed liquidation executor contract."""
    if not EXECUTOR_ADDRESS:
        raise EnvironmentError("EXECUTOR_ADDRESS is required for executor simulation or execution")

    return w3.eth.contract(
        address=Web3.to_checksum_address(EXECUTOR_ADDRESS),
        abi=EXECUTOR_ABI,
    )


def estimate_profit(
    w3: Web3,
    candidate: LiquidationCandidate,
    gas_cost_usd: float,
) -> dict:
    """
    Estimate profit from liquidating a position.
    Returns dict with gross_bonus, gas_cost, and profitability info.
    """
    loan_info = get_token_info(w3, candidate.market_params.loan_token)
    loan_decimals = loan_info["decimals"]

    bonus_human = candidate.estimated_bonus_in_loan_token / (10 ** loan_decimals)
    bonus_usd_approx = bonus_human
    net_profit_usd = bonus_usd_approx - gas_cost_usd

    return {
        "bonus_loan_token": bonus_human,
        "bonus_usd_approx": bonus_usd_approx,
        "gas_cost_usd": gas_cost_usd,
        "net_profit_usd": net_profit_usd,
        "profitable": net_profit_usd > MIN_PROFIT_USD,
    }


def estimate_seized_collateral(candidate: LiquidationCandidate) -> int:
    """
    Estimate collateral seized for full debt repayment.
    Morpho returns the exact value during execution; this estimate is only for
    Uniswap quote sizing before the executor eth_call.
    """
    if candidate.collateral_value_in_loan <= 0:
        return candidate.collateral

    seized_value_in_loan = candidate.borrow_assets + candidate.estimated_bonus_in_loan_token
    estimated = seized_value_in_loan * candidate.collateral // candidate.collateral_value_in_loan
    return max(1, min(candidate.collateral, estimated))


def min_profit_in_loan_token(w3: Web3, candidate: LiquidationCandidate) -> int:
    """
    Convert MIN_PROFIT_USD into loan-token base units. Stable symbols are treated
    as $1; non-stable symbols listed in LOAN_TOKEN_PRICES_USD are converted using
    their static USD price. Loan tokens with no configured price are rejected so
    the bot never under-prices the min-profit floor.
    """
    loan_info = get_token_info(w3, candidate.market_params.loan_token)
    symbol = loan_info["symbol"].upper()
    decimals = loan_info["decimals"]

    if symbol in STABLE_LOAN_TOKEN_SYMBOLS:
        return int(MIN_PROFIT_USD * (10 ** decimals))

    price_usd = LOAN_TOKEN_PRICES_USD.get(symbol)
    if not price_usd or price_usd <= 0:
        raise RouteUnavailable(
            f"No USD price configured for loan token {loan_info['symbol']}; "
            f"add it to LOAN_TOKEN_PRICES_USD before routing"
        )

    return int(MIN_PROFIT_USD / price_usd * (10 ** decimals))


def quote_uniswap_single_hop(
    w3: Web3,
    candidate: LiquidationCandidate,
    fee: int,
    amount_in: int,
) -> Optional[int]:
    """Quote collateral -> loan token through Uniswap V3 QuoterV2."""
    quoter = w3.eth.contract(
        address=Web3.to_checksum_address(UNISWAP_QUOTER_V2_ADDRESS),
        abi=QUOTER_V2_ABI,
    )
    params = (
        Web3.to_checksum_address(candidate.market_params.collateral_token),
        Web3.to_checksum_address(candidate.market_params.loan_token),
        amount_in,
        fee,
        0,
    )

    try:
        quoted = quoter.functions.quoteExactInputSingle(params).call()
        return int(quoted[0])
    except Exception as e:
        logger.debug(
            f"Uniswap quote failed for {candidate.collateral_token_symbol}/"
            f"{candidate.loan_token_symbol} fee={fee}: {e}"
        )
        return None


def build_executor_params(w3: Web3, candidate: LiquidationCandidate) -> tuple:
    """
    Build the ExecuteParams tuple expected by MorphoBlueLiquidationExecutor.
    Chooses the highest-output single-hop Uniswap V3 fee tier.
    """
    min_profit = min_profit_in_loan_token(w3, candidate)
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

    if best_fee is None or best_quote is None:
        raise RouteUnavailable(
            f"No Uniswap V3 single-hop quote for "
            f"{candidate.collateral_token_symbol}/{candidate.loan_token_symbol}"
        )

    amount_out_minimum = best_quote * (10_000 - SLIPPAGE_BPS) // 10_000
    logger.debug(
        f"Selected Uniswap V3 fee={best_fee} for "
        f"{candidate.collateral_token_symbol}/{candidate.loan_token_symbol}; "
        f"quote={best_quote}, minOut={amount_out_minimum}, amountIn={amount_in}"
    )

    return (
        candidate.market_params.as_tuple(),
        Web3.to_checksum_address(candidate.borrower),
        candidate.borrow_shares,
        best_fee,
        amount_out_minimum,
        min_profit,
    )


def log_candidate(
    candidate: LiquidationCandidate,
    profit_info: dict,
    log: logging.Logger,
) -> None:
    """Log a liquidation candidate with profitability details."""
    market_key = "0x" + candidate.market_id.hex()[:16] + "..."
    loan_info_str = candidate.loan_token_symbol or "?"
    coll_info_str = candidate.collateral_token_symbol or "?"
    comp_tag = f" | {candidate.competition_level}" if candidate.competition_level else ""
    stale_tag = " | STALE?" if candidate.is_stale else ""

    log.info(
        f"LIQUIDATABLE | Market: {market_key} ({coll_info_str}/{loan_info_str}) | "
        f"Borrower: {candidate.borrower} | "
        f"LTV: {candidate.ltv:.4f} > LLTV: {candidate.lltv:.4f} | "
        f"LIF: {candidate.lif:.4f} | "
        f"Debt: {profit_info['bonus_loan_token']:.2f} {loan_info_str} bonus | "
        f"Est. profit: ${profit_info['net_profit_usd']:.2f} "
        f"(bonus ${profit_info['bonus_usd_approx']:.2f} - gas ${profit_info['gas_cost_usd']:.2f}) | "
        f"Profitable: {profit_info['profitable']}{comp_tag}{stale_tag}"
    )


def simulate_liquidation(
    w3: Web3,
    morpho: Contract,
    candidate: LiquidationCandidate,
    from_address: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Simulate the full executor liquidation via eth_call.
    Returns (success, reason). If simulation reverts, returns (False, revert_reason).
    """
    del morpho

    try:
        executor = get_executor_contract(w3)
        params = build_executor_params(w3, candidate)
        result = executor.functions.executeLiquidation(params).call({
            "from": from_address or BOT_WALLET_ADDRESS or SIMULATION_FROM,
        })

        seized_assets, repaid_assets, profit = result
        if seized_assets == 0 or repaid_assets == 0:
            return False, "Simulation returned zero seized or repaid assets"

        logger.debug(
            f"Simulation OK: {candidate.borrower[:10]}... "
            f"seized={seized_assets}, repaid={repaid_assets}, profit={profit}"
        )
        return True, f"seized={seized_assets}, repaid={repaid_assets}, profit={profit}"

    except RouteUnavailable as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def execute_liquidation(
    w3: Web3,
    morpho: Contract,
    candidate: LiquidationCandidate,
) -> Optional[str]:
    """
    Execute a liquidation through MorphoBlueLiquidationExecutor after mandatory simulation.
    """
    if DRY_RUN:
        logger.info(
            f"DRY_RUN: Would call executor for {candidate.borrower[:10]}... "
            f"in market 0x{candidate.market_id.hex()[:16]}..."
        )
        return None

    if not gas_price_is_acceptable(w3):
        return None

    account = get_bot_account(w3)
    wallet_address = Web3.to_checksum_address(account.address)
    executor = get_executor_contract(w3)

    sim_ok, sim_reason = simulate_liquidation(w3, morpho, candidate, wallet_address)
    if not sim_ok:
        logger.warning(
            f"EXECUTION ABORTED: simulation failed for {candidate.borrower[:10]}...: "
            f"{sim_reason}"
        )
        return None

    gas_price = w3.eth.gas_price
    priority_fee = Web3.to_wei(2, "gwei")
    max_fee = gas_price + priority_fee
    params = build_executor_params(w3, candidate)

    logger.info(
        f"EXECUTION ATTEMPT | Executor: {EXECUTOR_ADDRESS} | "
        f"Market: 0x{candidate.market_id.hex()} | Borrower: {candidate.borrower} | "
        f"repaidShares: {candidate.borrow_shares} | wallet: {wallet_address} | "
        f"gas: {gas_price / 10**9:.1f} gwei"
    )

    tx = executor.functions.executeLiquidation(params).build_transaction({
        "from": wallet_address,
        "gas": LIQUIDATION_GAS_ESTIMATE,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "nonce": w3.eth.get_transaction_count(wallet_address),
        "chainId": w3.eth.chain_id,
    })

    signed = account.sign_transaction(tx)
    raw_tx = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw_tx)
    tx_hash_hex = tx_hash.hex()
    logger.info(f"LIQUIDATION SENT | tx: {tx_hash_hex}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    logger.info(
        f"LIQUIDATION COMPLETED | tx: {receipt.transactionHash.hex()} | "
        f"status: {receipt.status} | gasUsed: {receipt.gasUsed}"
    )
    return receipt.transactionHash.hex()


def process_candidates(
    w3: Web3,
    morpho: Contract,
    candidates: list[LiquidationCandidate],
    log: logging.Logger,
) -> None:
    """
    Process all liquidation candidates: estimate profit, check competition,
    simulate, log, and optionally execute. HIGH_COMPETITION positions are
    logged but skipped for execution.
    """
    if not candidates:
        return

    gas_cost_usd = estimate_gas_cost_usd(w3)

    profitable_candidates = [
        c for c in candidates
        if estimate_profit(w3, c, gas_cost_usd)["profitable"]
    ]
    top_for_competition = profitable_candidates[:COMPETITION_CHECK_TOP_N]

    if top_for_competition:
        current_block = w3.eth.block_number
        borrower_addrs = [c.borrower for c in top_for_competition]
        competition_map = check_competition_batch(current_block, borrower_addrs)

        for candidate in candidates:
            if candidate.borrower in competition_map:
                candidate.competition_level = competition_map[candidate.borrower]

    profitable_count = 0
    sim_pass_count = 0
    skipped_high_comp = 0

    for candidate in candidates:
        profit_info = estimate_profit(w3, candidate, gas_cost_usd)
        log_candidate(candidate, profit_info, log)

        if candidate.is_stale:
            log.warning(
                "  ^ Position flagged as potentially stale: deeply underwater, "
                "likely already liquidated by a competing bot"
            )

        if candidate.competition_level == HIGH:
            log.warning(
                "  ^ HIGH_COMPETITION: MEV bots actively targeting this borrower. Deprioritized."
            )

        if not profit_info["profitable"]:
            continue

        profitable_count += 1

        if candidate.competition_level == HIGH:
            skipped_high_comp += 1
            log.info(
                f"Skipping execution for {candidate.borrower[:10]}... "
                "(HIGH_COMPETITION: MEV bots are targeting this position)"
            )
            continue

        collateral_addr = Web3.to_checksum_address(candidate.market_params.collateral_token)
        if collateral_addr not in ALLOWED_COLLATERAL_TOKENS:
            log.debug(
                f"Skipping {candidate.borrower[:10]}...: collateral {collateral_addr} "
                f"not in ALLOWED_COLLATERAL_TOKENS"
            )
            continue

        if not gas_price_is_acceptable(w3):
            continue

        sim_ok, sim_reason = simulate_liquidation(w3, morpho, candidate)
        if not sim_ok:
            log.warning(
                f"SIMULATION FAILED for {candidate.borrower[:10]}...: {sim_reason}. Skipping."
            )
            continue

        sim_pass_count += 1
        log.info(f"SIMULATION OK for {candidate.borrower[:10]}...: {sim_reason}")

        execute_liquidation(w3, morpho, candidate)

    if profitable_count > 0:
        log.info(
            f"{profitable_count}/{len(candidates)} profitable, "
            f"{sim_pass_count}/{profitable_count} passed simulation, "
            f"{skipped_high_comp} skipped (HIGH_COMPETITION) "
            f"(above ${MIN_PROFIT_USD} threshold)"
        )
