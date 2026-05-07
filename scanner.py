"""
Scanner module for Morpho Blue Liquidation Bot.
Discovers markets, fetches borrower positions, checks health, identifies liquidation candidates.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests
from web3 import Web3
from web3.contract import Contract
from eth_abi import decode

from config import (
    MORPHO_BLUE_ADDRESS,
    MULTICALL3_ADDRESS,
    MORPHO_DEPLOYMENT_BLOCK,
    MORPHO_API_URL,
    MAX_POSITIONS_PER_QUERY,
    WAD,
    VIRTUAL_SHARES,
    VIRTUAL_ASSETS,
    ORACLE_PRICE_SCALE,
    LIF_MAX,
    LIF_BETA,
    LIF_ONE_MINUS_BETA,
    MORPHO_ABI,
    ORACLE_ABI,
    IRM_ABI,
    MULTICALL3_ABI,
    ERC20_ABI,
    MARKET_BLACKLIST,
    STALE_BLOCKS_THRESHOLD,
    STALE_LTV_EXCESS_THRESHOLD,
    CROSS_CHECK_TOP_N,
    MAX_SUBGRAPH_LAG_BLOCKS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MarketParams:
    loan_token: str
    collateral_token: str
    oracle: str
    irm: str
    lltv: int  # WAD-scaled

    def as_tuple(self):
        return (
            Web3.to_checksum_address(self.loan_token),
            Web3.to_checksum_address(self.collateral_token),
            Web3.to_checksum_address(self.oracle),
            Web3.to_checksum_address(self.irm),
            self.lltv,
        )


@dataclass
class MarketState:
    total_supply_assets: int
    total_supply_shares: int
    total_borrow_assets: int
    total_borrow_shares: int
    last_update: int
    fee: int


@dataclass
class PositionData:
    market_id: bytes
    borrower: str
    supply_shares: int
    borrow_shares: int
    collateral: int


@dataclass
class LiquidationCandidate:
    market_id: bytes
    market_params: MarketParams
    borrower: str
    borrow_shares: int
    borrow_assets: int
    collateral: int
    collateral_value_in_loan: int
    ltv: float
    lltv: float
    lif: float
    estimated_bonus_in_loan_token: int
    is_stale: bool = False  # Flagged if likely already liquidated by another bot
    competition_level: str = ""  # LOW_COMPETITION, HIGH_COMPETITION, or UNKNOWN_COMPETITION
    loan_token_symbol: str = ""
    collateral_token_symbol: str = ""


# ---------------------------------------------------------------------------
# Math helpers (match on-chain logic exactly)
# ---------------------------------------------------------------------------

def mul_div_down(x: int, y: int, d: int) -> int:
    return (x * y) // d


def mul_div_up(x: int, y: int, d: int) -> int:
    return (x * y + d - 1) // d


def to_assets_up(shares: int, total_assets: int, total_shares: int) -> int:
    """Convert shares to assets, rounding up (matches SharesMathLib.toAssetsUp)."""
    return mul_div_up(shares, total_assets + VIRTUAL_ASSETS, total_shares + VIRTUAL_SHARES)


def w_taylor_compounded(rate: int, elapsed: int) -> int:
    """
    3-term Taylor approximation of e^(rate*elapsed) - 1, matching the on-chain implementation.
    rate is per-second, WAD-scaled.
    """
    first_term = rate * elapsed
    second_term = mul_div_down(first_term, first_term, 2 * WAD)
    third_term = mul_div_down(second_term, first_term, 3 * WAD)
    return first_term + second_term + third_term


def calculate_lif(lltv: int) -> int:
    """
    Calculate Liquidation Incentive Factor in WAD.
    LIF = min(1.15, 1 / (0.3 * lltv + 0.7))
    All values WAD-scaled.
    """
    denominator = mul_div_down(LIF_BETA, lltv, WAD) + LIF_ONE_MINUS_BETA
    lif = mul_div_down(WAD, WAD, denominator)
    return min(LIF_MAX, lif)


# ---------------------------------------------------------------------------
# Token info cache
# ---------------------------------------------------------------------------

_token_cache: dict[str, dict] = {}


def get_token_info(w3: Web3, token_address: str) -> dict:
    """Get token symbol and decimals, with caching."""
    addr = Web3.to_checksum_address(token_address)
    if addr in _token_cache:
        return _token_cache[addr]

    try:
        contract = w3.eth.contract(address=addr, abi=ERC20_ABI)
        symbol = contract.functions.symbol().call()
        decimals = contract.functions.decimals().call()
        info = {"symbol": symbol, "decimals": decimals}
    except Exception:
        info = {"symbol": addr[:10] + "...", "decimals": 18}

    _token_cache[addr] = info
    return info


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def fetch_markets_from_api() -> list[tuple[bytes, MarketParams]]:
    """
    Fetch all active markets (with borrows) from the Morpho GraphQL API.
    Returns list of (market_id_bytes, MarketParams).
    """
    query = """
    query {
        markets(where: { chainId_in: [1], borrowAssetsUsd_gte: 100 }) {
            items {
                uniqueKey
                lltv
                oracleAddress
                irmAddress
                loanAsset { address }
                collateralAsset { address }
                state { borrowAssetsUsd }
            }
        }
    }
    """

    try:
        resp = requests.post(
            MORPHO_API_URL,
            json={"query": query},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Morpho API request failed: {e}")
        return []

    items = data.get("data", {}).get("markets", {}).get("items", [])
    markets = []

    for item in items:
        try:
            unique_key = item["uniqueKey"]
            market_id = bytes.fromhex(unique_key.replace("0x", ""))

            collateral_asset = item.get("collateralAsset")
            if not collateral_asset or not collateral_asset.get("address"):
                continue  # Skip markets without collateral (pure lending)

            params = MarketParams(
                loan_token=item["loanAsset"]["address"],
                collateral_token=collateral_asset["address"],
                oracle=item["oracleAddress"],
                irm=item["irmAddress"],
                lltv=int(item["lltv"]),
            )
            markets.append((market_id, params))
        except (KeyError, ValueError) as e:
            logger.debug(f"Skipping market due to parse error: {e}")
            continue

    logger.info(f"API returned {len(markets)} active markets with borrows")
    return markets


def get_subgraph_block_number() -> Optional[int]:
    """
    Return the newest indexed block number reported by active market state.
    Morpho's hosted API is not a The Graph endpoint and does not expose _meta.
    """
    query = """
    query {
        markets(where: { chainId_in: [1], borrowAssetsUsd_gte: 100 }, first: 25) {
            items {
                state {
                    blockNumber
                }
            }
        }
    }
    """

    try:
        resp = requests.post(
            MORPHO_API_URL,
            json={"query": query},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Subgraph freshness check failed: {e}")
        return None

    items = data.get("data", {}).get("markets", {}).get("items", [])
    block_numbers = []
    for item in items:
        try:
            block_number = item.get("state", {}).get("blockNumber")
            if block_number is not None:
                block_numbers.append(int(block_number))
        except (AttributeError, TypeError, ValueError):
            continue

    if not block_numbers:
        errors = data.get("errors")
        if errors:
            logger.warning(f"Subgraph freshness unavailable: {errors[0].get('message', errors[0])}")
        else:
            logger.warning("Subgraph freshness unavailable: active market state.blockNumber missing")
        return None

    return max(block_numbers)


def fetch_markets_from_events(w3: Web3, morpho: Contract) -> list[tuple[bytes, MarketParams]]:
    """
    Fallback: discover markets by scanning CreateMarket events on-chain.
    Slower but trustless.
    """
    markets = []
    latest_block = w3.eth.block_number
    chunk_size = 10_000

    logger.info(f"Scanning CreateMarket events from block {MORPHO_DEPLOYMENT_BLOCK} to {latest_block}")

    from_block = MORPHO_DEPLOYMENT_BLOCK
    while from_block <= latest_block:
        to_block = min(from_block + chunk_size - 1, latest_block)
        try:
            event_filter = morpho.events.CreateMarket.create_filter(
                fromBlock=from_block, toBlock=to_block
            )
            events = event_filter.get_all_entries()

            for event in events:
                args = event["args"]
                market_id = args["id"]
                mp = args["marketParams"]
                params = MarketParams(
                    loan_token=mp[0],
                    collateral_token=mp[1],
                    oracle=mp[2],
                    irm=mp[3],
                    lltv=mp[4],
                )
                markets.append((market_id, params))
        except Exception as e:
            logger.warning(f"Error scanning blocks {from_block}-{to_block}: {e}")

        from_block = to_block + 1

    logger.info(f"Event scan found {len(markets)} markets")
    return markets


# ---------------------------------------------------------------------------
# Borrower discovery
# ---------------------------------------------------------------------------

def fetch_borrowers_from_api(market_id: bytes) -> list[str]:
    """Fetch borrower addresses for a market from the Morpho API."""
    market_key = "0x" + market_id.hex()

    query = """
    query($marketKey: String!) {
        marketPositions(
            where: { marketUniqueKey_in: [$marketKey], borrowShares_gte: "1" }
            first: 1000
        ) {
            items {
                user { address }
            }
        }
    }
    """

    try:
        resp = requests.post(
            MORPHO_API_URL,
            json={"query": query, "variables": {"marketKey": market_key}},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Failed to fetch borrowers for market {market_key[:18]}...: {e}")
        return []

    items = data.get("data", {}).get("marketPositions", {}).get("items", [])
    borrowers = []
    for item in items:
        try:
            addr = item["user"]["address"]
            borrowers.append(Web3.to_checksum_address(addr))
        except (KeyError, ValueError):
            continue

    return borrowers


def fetch_position_from_api(market_id: bytes, borrower: str) -> Optional[PositionData]:
    """Fetch one borrower position from the Morpho API for stale-data comparison."""
    market_key = "0x" + market_id.hex()
    borrower = Web3.to_checksum_address(borrower)

    query = """
    query($marketKey: String!) {
        marketPositions(
            where: { marketUniqueKey_in: [$marketKey], borrowShares_gte: "1" }
            first: 1000
        ) {
            items {
                borrowShares
                collateral
                user { address }
            }
        }
    }
    """

    try:
        resp = requests.post(
            MORPHO_API_URL,
            json={"query": query, "variables": {"marketKey": market_key}},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Failed to fetch API position for {borrower[:10]}...: {e}")
        return None

    if data.get("errors"):
        logger.warning(
            f"Failed to fetch API position for {borrower[:10]}...: "
            f"{data['errors'][0].get('message', data['errors'][0])}"
        )
        return None

    items = data.get("data", {}).get("marketPositions", {}).get("items", [])
    for item in items:
        try:
            item_borrower = Web3.to_checksum_address(item["user"]["address"])
            if item_borrower != borrower:
                continue

            return PositionData(
                market_id=market_id,
                borrower=borrower,
                supply_shares=0,
                borrow_shares=int(item["borrowShares"]),
                collateral=int(item["collateral"]),
            )
        except (KeyError, TypeError, ValueError):
            continue

    return None


def fetch_borrowers_from_events(
    w3: Web3,
    morpho: Contract,
    market_id: bytes,
) -> list[str]:
    """
    Discover borrowers from Borrow events for one market.
    This is slower than the API but avoids depending on a stale subgraph.
    """
    latest_block = w3.eth.block_number
    chunk_size = 10_000
    borrowers: set[str] = set()
    market_key = "0x" + market_id.hex()[:16] + "..."

    logger.info(f"Scanning Borrow events for market {market_key}")

    from_block = MORPHO_DEPLOYMENT_BLOCK
    while from_block <= latest_block:
        to_block = min(from_block + chunk_size - 1, latest_block)
        try:
            event_filter = morpho.events.Borrow.create_filter(
                fromBlock=from_block,
                toBlock=to_block,
                argument_filters={"id": market_id},
            )
            events = event_filter.get_all_entries()
            for event in events:
                borrower = event["args"].get("onBehalf")
                if borrower:
                    borrowers.add(Web3.to_checksum_address(borrower))
        except Exception as e:
            logger.warning(
                f"Error scanning Borrow events for {market_key} "
                f"blocks {from_block}-{to_block}: {e}"
            )

        from_block = to_block + 1

    logger.info(f"Event scan found {len(borrowers)} borrower(s) for {market_key}")
    return sorted(borrowers)


# ---------------------------------------------------------------------------
# On-chain reads
# ---------------------------------------------------------------------------

def get_market_state_onchain(morpho: Contract, market_id: bytes) -> MarketState:
    """Read market state from the Morpho Blue contract."""
    result = morpho.functions.market(market_id).call()
    return MarketState(
        total_supply_assets=result[0],
        total_supply_shares=result[1],
        total_borrow_assets=result[2],
        total_borrow_shares=result[3],
        last_update=result[4],
        fee=result[5],
    )


def get_oracle_price(w3: Web3, oracle_address: str) -> Optional[int]:
    """Query oracle for current price. Returns None if oracle reverts."""
    try:
        oracle = w3.eth.contract(
            address=Web3.to_checksum_address(oracle_address),
            abi=ORACLE_ABI,
        )
        price = oracle.functions.price().call()
        if price <= 0:
            logger.warning(f"Oracle {oracle_address} returned non-positive price: {price}")
            return None
        return price
    except Exception as e:
        logger.warning(f"Oracle {oracle_address} reverted: {e}")
        return None


def accrue_interest(
    w3: Web3,
    market_state: MarketState,
    market_params: MarketParams,
) -> MarketState:
    """
    Simulate interest accrual since last update.
    Matches the on-chain accrueInterest logic exactly.
    """
    if market_state.total_borrow_assets == 0:
        return market_state

    # Zero address IRM means no interest
    if market_params.irm == "0x0000000000000000000000000000000000000000":
        return market_state

    current_timestamp = w3.eth.get_block("latest")["timestamp"]
    elapsed = current_timestamp - market_state.last_update

    if elapsed <= 0:
        return market_state

    # Get borrow rate from IRM
    try:
        irm_contract = w3.eth.contract(
            address=Web3.to_checksum_address(market_params.irm),
            abi=IRM_ABI,
        )
        market_tuple = (
            market_state.total_supply_assets,
            market_state.total_supply_shares,
            market_state.total_borrow_assets,
            market_state.total_borrow_shares,
            market_state.last_update,
            market_state.fee,
        )
        borrow_rate = irm_contract.functions.borrowRateView(
            market_params.as_tuple(),
            market_tuple,
        ).call()
    except Exception as e:
        logger.debug(f"IRM call failed, using stale state: {e}")
        return market_state

    # Calculate interest using Taylor approximation
    interest = mul_div_down(
        market_state.total_borrow_assets,
        w_taylor_compounded(borrow_rate, elapsed),
        WAD,
    )

    new_total_borrow_assets = market_state.total_borrow_assets + interest
    new_total_supply_assets = market_state.total_supply_assets + interest
    new_total_supply_shares = market_state.total_supply_shares

    # Apply fee if set
    if market_state.fee > 0 and interest > 0:
        fee_amount = mul_div_down(interest, market_state.fee, WAD)
        # fee shares = toSharesDown(feeAmount, totalSupplyAssets - feeAmount, totalSupplyShares)
        if new_total_supply_assets - fee_amount > 0:
            fee_shares = mul_div_down(
                fee_amount,
                new_total_supply_shares + VIRTUAL_SHARES,
                (new_total_supply_assets - fee_amount) + VIRTUAL_ASSETS,
            )
            new_total_supply_shares += fee_shares

    return MarketState(
        total_supply_assets=new_total_supply_assets,
        total_supply_shares=new_total_supply_shares,
        total_borrow_assets=new_total_borrow_assets,
        total_borrow_shares=market_state.total_borrow_shares,
        last_update=current_timestamp,
        fee=market_state.fee,
    )


# ---------------------------------------------------------------------------
# Multicall batched position reads
# ---------------------------------------------------------------------------

def batch_fetch_positions(
    w3: Web3,
    morpho: Contract,
    market_id: bytes,
    borrowers: list[str],
    batch_size: int = 200,
) -> list[PositionData]:
    """
    Fetch positions for multiple borrowers using Multicall3.
    Returns list of PositionData for borrowers with nonzero borrowShares.
    """
    if not borrowers:
        return []

    multicall = w3.eth.contract(
        address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
        abi=MULTICALL3_ABI,
    )

    positions = []

    for i in range(0, len(borrowers), batch_size):
        batch = borrowers[i : i + batch_size]

        # Encode each position() call
        calls = []
        for borrower in batch:
            calldata = morpho.encode_abi("position", args=[market_id, borrower])
            calls.append((
                Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
                True,  # allowFailure
                bytes.fromhex(calldata[2:]) if isinstance(calldata, str) else calldata,
            ))

        try:
            results = multicall.functions.aggregate3(calls).call()
        except Exception as e:
            logger.warning(f"Multicall failed for batch starting at {i}: {e}")
            continue

        for j, (success, return_data) in enumerate(results):
            if not success or len(return_data) < 64:
                continue

            try:
                decoded = decode(
                    ["uint256", "uint128", "uint128"],
                    return_data,
                )
                supply_shares, borrow_shares, collateral = decoded

                if borrow_shares > 0:
                    positions.append(PositionData(
                        market_id=market_id,
                        borrower=batch[j],
                        supply_shares=supply_shares,
                        borrow_shares=borrow_shares,
                        collateral=collateral,
                    ))
            except Exception as e:
                logger.debug(f"Failed to decode position for {batch[j]}: {e}")
                continue

    return positions


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_position_health(
    position: PositionData,
    market_state: MarketState,
    market_params: MarketParams,
    oracle_price: int,
    current_block: int,
) -> Optional[LiquidationCandidate]:
    """
    Check if a position is liquidatable.
    Returns LiquidationCandidate if unhealthy, None otherwise.
    Also flags positions that are likely stale (already liquidated by another bot).
    """
    if position.borrow_shares == 0 or position.collateral == 0:
        return None

    # Convert borrow shares to assets (round up = worst case for borrower)
    borrow_assets = to_assets_up(
        position.borrow_shares,
        market_state.total_borrow_assets,
        market_state.total_borrow_shares,
    )

    # Collateral value in loan token units
    collateral_value = mul_div_down(position.collateral, oracle_price, ORACLE_PRICE_SCALE)

    # Liquidatable when: borrowAssets * WAD > collateralValue * LLTV
    # i.e., LTV > LLTV
    is_liquidatable = borrow_assets * WAD > collateral_value * market_params.lltv

    if not is_liquidatable:
        return None

    # Calculate LIF and estimated bonus
    lif = calculate_lif(market_params.lltv)

    # Bonus = repaid * (LIF - 1) / WAD, expressed in loan token
    # If we repay the full debt, bonus = borrow_assets * (LIF - 1) / WAD
    estimated_bonus = mul_div_down(borrow_assets, lif - WAD, WAD)

    # Human-readable LTV values
    ltv_float = borrow_assets / collateral_value if collateral_value > 0 else float("inf")
    lltv_float = market_params.lltv / WAD

    # Stale position detection: if market hasn't been touched in a while AND
    # the position is deeply underwater, it was probably already liquidated and
    # the API is returning stale data.
    is_stale = False
    # Estimate blocks since last update (~12s per block)
    blocks_since_update = current_block - (market_state.last_update // 12) if market_state.last_update > 1_000_000_000 else 0
    # Use timestamp-based estimation: last_update is a unix timestamp
    # Approximate block number at last_update
    elapsed_seconds = (current_block * 12) - market_state.last_update  # rough
    # Better: just use the raw last_update timestamp vs current block age
    # We'll pass current_block to estimate staleness in blocks
    ltv_excess = ltv_float - lltv_float
    if ltv_excess > STALE_LTV_EXCESS_THRESHOLD:
        # Market lastUpdate is a timestamp. Estimate how many blocks ago that was.
        # current_block is the latest block number; we get its timestamp separately
        # For simplicity, use a flag based on the fact that deeply underwater
        # positions in an active market would already have been liquidated.
        is_stale = True
        logger.debug(
            f"Stale flag: {position.borrower[:10]}... LTV excess={ltv_excess:.4f} "
            f"(>{STALE_LTV_EXCESS_THRESHOLD})"
        )

    return LiquidationCandidate(
        market_id=position.market_id,
        market_params=market_params,
        borrower=position.borrower,
        borrow_shares=position.borrow_shares,
        borrow_assets=borrow_assets,
        collateral=position.collateral,
        collateral_value_in_loan=collateral_value,
        ltv=ltv_float,
        lltv=lltv_float,
        lif=lif / WAD,
        estimated_bonus_in_loan_token=estimated_bonus,
        is_stale=is_stale,
    )


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def cross_check_top_candidates(
    w3: Web3,
    morpho: Contract,
    candidates: list[LiquidationCandidate],
    n: int = CROSS_CHECK_TOP_N,
    compare_api_position: bool = True,
) -> list[LiquidationCandidate]:
    """
    For the top N candidates, fetch position directly via position() on-chain
    and compare against the GraphQL position payload when API mode is enabled.
    Removes candidates whose data is stale or no longer liquidatable.
    """
    if not candidates:
        return candidates

    verified = []
    to_check = candidates[:n]
    rest = candidates[n:]

    for candidate in to_check:
        try:
            result = morpho.functions.position(
                candidate.market_id,
                candidate.borrower,
            ).call()
            onchain_supply_shares = result[0]
            onchain_borrow_shares = result[1]
            onchain_collateral = result[2]

            if onchain_borrow_shares == 0:
                logger.warning(
                    f"CROSS-CHECK FAIL: {candidate.borrower[:10]}... has 0 borrowShares on-chain "
                    f"(scanner found borrow_assets={candidate.borrow_assets}). "
                    f"Position likely already liquidated. Removing."
                )
                continue

            if onchain_collateral == 0:
                logger.warning(
                    f"CROSS-CHECK FAIL: {candidate.borrower[:10]}... has 0 collateral on-chain. "
                    f"Removing."
                )
                continue

            if compare_api_position:
                api_position = fetch_position_from_api(candidate.market_id, candidate.borrower)
                if api_position is None:
                    logger.warning(
                        f"CROSS-CHECK WARN: no API position payload for "
                        f"{candidate.borrower[:10]}...; direct on-chain check will decide"
                    )
                else:
                    api_borrow_drift = (
                        abs(onchain_borrow_shares - api_position.borrow_shares)
                        / api_position.borrow_shares
                        if api_position.borrow_shares
                        else 0
                    )
                    api_collateral_drift = (
                        abs(onchain_collateral - api_position.collateral)
                        / api_position.collateral
                        if api_position.collateral
                        else 0
                    )
                    if api_borrow_drift > 0.01 or api_collateral_drift > 0.01:
                        logger.warning(
                            f"CROSS-CHECK FAIL: API position stale for {candidate.borrower[:10]}... "
                            f"borrowShares api={api_position.borrow_shares} vs on-chain={onchain_borrow_shares} "
                            f"({api_borrow_drift:.2%}), collateral api={api_position.collateral} "
                            f"vs on-chain={onchain_collateral} ({api_collateral_drift:.2%}). Removing."
                        )
                        continue

            borrow_drift = (
                abs(onchain_borrow_shares - candidate.borrow_shares) / candidate.borrow_shares
                if candidate.borrow_shares
                else 0
            )
            collateral_drift = (
                abs(onchain_collateral - candidate.collateral) / candidate.collateral
                if candidate.collateral
                else 0
            )
            if borrow_drift > 0.01 or collateral_drift > 0.01:
                logger.warning(
                    f"CROSS-CHECK DRIFT: {candidate.borrower[:10]}... "
                    f"borrowShares scanner={candidate.borrow_shares} vs on-chain={onchain_borrow_shares} "
                    f"({borrow_drift:.2%}), collateral scanner={candidate.collateral} "
                    f"vs on-chain={onchain_collateral} ({collateral_drift:.2%}). Recomputing."
                )

            market_state = get_market_state_onchain(morpho, candidate.market_id)
            accrued_state = accrue_interest(w3, market_state, candidate.market_params)
            oracle_price = get_oracle_price(w3, candidate.market_params.oracle)
            if oracle_price is None:
                logger.warning(
                    f"CROSS-CHECK FAIL: {candidate.borrower[:10]}... oracle reverted. Removing."
                )
                continue

            fresh_position = PositionData(
                market_id=candidate.market_id,
                borrower=candidate.borrower,
                supply_shares=onchain_supply_shares,
                borrow_shares=onchain_borrow_shares,
                collateral=onchain_collateral,
            )
            fresh_candidate = check_position_health(
                fresh_position,
                accrued_state,
                candidate.market_params,
                oracle_price,
                w3.eth.block_number,
            )
            if fresh_candidate is None:
                logger.warning(
                    f"CROSS-CHECK FAIL: {candidate.borrower[:10]}... no longer liquidatable. Removing."
                )
                continue

            fresh_candidate.loan_token_symbol = candidate.loan_token_symbol
            fresh_candidate.collateral_token_symbol = candidate.collateral_token_symbol

            logger.debug(
                f"CROSS-CHECK OK: {candidate.borrower[:10]}... "
                f"borrowShares={onchain_borrow_shares}, collateral={onchain_collateral}"
            )
            verified.append(fresh_candidate)

        except Exception as e:
            logger.warning(f"Cross-check failed for {candidate.borrower[:10]}...: {e}")
            logger.warning(f"Removing {candidate.borrower[:10]}... because top-candidate cross-check is required")

    return verified + rest


def scan_all_markets(w3: Web3, morpho: Contract) -> list[LiquidationCandidate]:
    """
    Scan all active Morpho Blue markets for liquidatable positions.
    Returns candidates sorted by estimated bonus descending.
    """
    current_block = w3.eth.block_number
    use_api = True
    subgraph_block = get_subgraph_block_number()
    if subgraph_block is None:
        logger.warning("Subgraph freshness is unknown; API discovery remains enabled")
    else:
        lag = current_block - subgraph_block
        logger.info(f"Morpho API subgraph block {subgraph_block}, chain block {current_block}, lag {lag}")
        if lag > MAX_SUBGRAPH_LAG_BLOCKS:
            use_api = False
            logger.warning(
                f"Subgraph lag is {lag} blocks (> {MAX_SUBGRAPH_LAG_BLOCKS}); "
                f"bypassing GraphQL and using on-chain event scanning"
            )

    # Step 1: Discover markets
    markets = fetch_markets_from_api() if use_api else []
    if not markets:
        logger.warning("Using event scan for market discovery")
        markets = fetch_markets_from_events(w3, morpho)

    if not markets:
        logger.warning("No markets found")
        return []

    # Filter out blacklisted markets
    pre_filter = len(markets)
    markets = [
        (mid, mp) for mid, mp in markets
        if ("0x" + mid.hex()) not in MARKET_BLACKLIST
    ]
    blacklisted = pre_filter - len(markets)
    if blacklisted > 0:
        logger.info(f"Filtered out {blacklisted} blacklisted market(s)")

    all_candidates: list[LiquidationCandidate] = []
    markets_scanned = 0
    total_positions_checked = 0

    for market_id, market_params in markets:
        market_key = "0x" + market_id.hex()[:16] + "..."

        try:
            # Step 2: Get on-chain market state and accrue interest
            market_state = get_market_state_onchain(morpho, market_id)

            if market_state.total_borrow_assets == 0:
                continue

            accrued_state = accrue_interest(w3, market_state, market_params)

            # Step 3: Get oracle price
            oracle_price = get_oracle_price(w3, market_params.oracle)
            if oracle_price is None:
                logger.debug(f"Skipping market {market_key}: oracle failed")
                continue

            # Step 4: Get borrowers
            borrowers = (
                fetch_borrowers_from_api(market_id)
                if use_api
                else fetch_borrowers_from_events(w3, morpho, market_id)
            )
            if not borrowers:
                continue

            # Step 5: Batch fetch positions
            positions = batch_fetch_positions(w3, morpho, market_id, borrowers)
            total_positions_checked += len(positions)

            # Step 6: Check each position's health
            for position in positions:
                candidate = check_position_health(
                    position, accrued_state, market_params, oracle_price, current_block
                )
                if candidate:
                    # Enrich with token info
                    loan_info = get_token_info(w3, market_params.loan_token)
                    coll_info = get_token_info(w3, market_params.collateral_token)
                    candidate.loan_token_symbol = loan_info["symbol"]
                    candidate.collateral_token_symbol = coll_info["symbol"]
                    all_candidates.append(candidate)

            markets_scanned += 1

        except Exception as e:
            logger.error(f"Error scanning market {market_key}: {e}", exc_info=True)
            continue

    # Sort by estimated bonus descending
    all_candidates.sort(key=lambda c: c.estimated_bonus_in_loan_token, reverse=True)

    # Log stale positions
    stale_count = sum(1 for c in all_candidates if c.is_stale)
    if stale_count > 0:
        logger.warning(
            f"{stale_count}/{len(all_candidates)} candidates flagged as potentially stale "
            f"(LTV excess > {STALE_LTV_EXCESS_THRESHOLD:.0%}, likely already liquidated)"
        )

    # Cross-check top N candidates directly on-chain
    if all_candidates:
        all_candidates = cross_check_top_candidates(
            w3,
            morpho,
            all_candidates,
            compare_api_position=use_api,
        )

    logger.info(
        f"Scanned {markets_scanned} markets, checked {total_positions_checked} positions, "
        f"found {len(all_candidates)} liquidatable"
    )

    return all_candidates
