"""
MEV competition checker for Morpho Blue Liquidation Bot.
Queries Etherscan for recent liquidation attempts targeting the same borrowers,
and flags positions that known MEV bots are actively competing for.
"""

import logging
import time
from typing import Optional

import requests
from web3 import Web3

from config import (
    ETHERSCAN_API_KEY,
    ETHERSCAN_API_URL,
    MORPHO_BLUE_ADDRESS,
    KNOWN_MEV_BOTS,
    COMPETITION_LOOKBACK_BLOCKS,
    LIQUIDATE_SELECTOR,
)

logger = logging.getLogger(__name__)

# Competition levels
LOW = "LOW_COMPETITION"
HIGH = "HIGH_COMPETITION"
UNKNOWN = "UNKNOWN_COMPETITION"

# Rate-limit: Etherscan free tier = 5 calls/sec
_last_etherscan_call = 0.0
_ETHERSCAN_INTERVAL = 0.22  # ~4.5 calls/sec to stay safe


def _rate_limited_etherscan_get(params: dict) -> Optional[dict]:
    """Make a rate-limited GET request to the Etherscan API."""
    global _last_etherscan_call

    if not ETHERSCAN_API_KEY:
        return None

    params["apikey"] = ETHERSCAN_API_KEY

    now = time.monotonic()
    wait = _ETHERSCAN_INTERVAL - (now - _last_etherscan_call)
    if wait > 0:
        time.sleep(wait)

    try:
        resp = requests.get(ETHERSCAN_API_URL, params=params, timeout=10)
        _last_etherscan_call = time.monotonic()
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "0" and "No transactions found" in data.get("message", ""):
            return {"result": []}
        if data.get("status") == "0":
            logger.debug(f"Etherscan API error: {data.get('message')}")
            return None
        return data
    except Exception as e:
        logger.debug(f"Etherscan request failed: {e}")
        return None


def get_recent_liquidation_txs_to_morpho(
    from_block: int,
) -> list[dict]:
    """
    Fetch recent transactions TO the Morpho Blue contract starting from from_block.
    We then filter for liquidate() calls and check the sender against known MEV bots.
    Uses Etherscan's txlist endpoint.
    """
    data = _rate_limited_etherscan_get({
        "module": "account",
        "action": "txlist",
        "address": MORPHO_BLUE_ADDRESS,
        "startblock": from_block,
        "endblock": 99999999,
        "page": 1,
        "offset": 200,  # last 200 txs to Morpho in this block range
        "sort": "desc",
    })

    if data is None:
        return []

    return data.get("result", []) if isinstance(data.get("result"), list) else []


def get_recent_txs_from_address(
    address: str,
    from_block: int,
) -> list[dict]:
    """
    Fetch recent outgoing transactions FROM a specific address (e.g. a known MEV bot)
    that go TO the Morpho Blue contract.
    """
    data = _rate_limited_etherscan_get({
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": from_block,
        "endblock": 99999999,
        "page": 1,
        "offset": 50,
        "sort": "desc",
    })

    if data is None:
        return []

    results = data.get("result", []) if isinstance(data.get("result"), list) else []

    # Filter to only txs sent TO Morpho Blue
    morpho_lower = MORPHO_BLUE_ADDRESS.lower()
    return [tx for tx in results if tx.get("to", "").lower() == morpho_lower]


def extract_borrower_from_calldata(input_data: str) -> Optional[str]:
    """
    Extract the borrower address from a liquidate() call's input data.
    liquidate(MarketParams, address borrower, uint256, uint256, bytes)
    MarketParams is a tuple of 5 fields (5 * 32 = 160 bytes).
    The borrower address is the 6th word (bytes 164-196 from start of params).
    Selector = 4 bytes, then params start.
    Word layout: words 0-4 = MarketParams (5 slots), word 5 = borrower.
    """
    if not input_data or len(input_data) < 10:
        return None

    selector = input_data[:10].lower()
    if selector != LIQUIDATE_SELECTOR:
        return None

    # Each word is 64 hex chars (32 bytes). Borrower is word index 5.
    # Offset: 10 (selector) + 5*64 = 10 + 320 = 330
    # Borrower word: chars 330..394
    if len(input_data) < 394:
        return None

    borrower_word = input_data[330:394]
    try:
        address = Web3.to_checksum_address("0x" + borrower_word[-40:])
        return address
    except Exception:
        return None


def check_competition_batch(
    current_block: int,
    borrower_addresses: list[str],
) -> dict[str, str]:
    """
    Check MEV competition for a batch of borrower addresses.

    Strategy:
    1. Fetch recent txs TO Morpho Blue contract in the lookback window.
    2. Filter for liquidate() calls.
    3. Check if the borrower in the calldata matches any of our targets.
    4. Check if the sender is a known MEV bot.

    Returns dict mapping borrower address -> competition level (HIGH/LOW/UNKNOWN).
    """
    if not ETHERSCAN_API_KEY:
        logger.warning(
            "ETHERSCAN_API_KEY not set — competition check disabled. "
            "Add it to .env for MEV competition detection."
        )
        return {addr: UNKNOWN for addr in borrower_addresses}

    from_block = max(0, current_block - COMPETITION_LOOKBACK_BLOCKS)
    borrower_set = {addr.lower() for addr in borrower_addresses}
    results: dict[str, str] = {addr: LOW for addr in borrower_addresses}

    # --- Approach 1: Scan recent txs TO Morpho for liquidate() calls ---
    logger.debug(f"Fetching recent Morpho txs from block {from_block}")
    recent_txs = get_recent_liquidation_txs_to_morpho(from_block)

    competing_senders: set[str] = set()
    targeted_borrowers: set[str] = set()

    for tx in recent_txs:
        input_data = tx.get("input", "")
        if not input_data.lower().startswith(LIQUIDATE_SELECTOR):
            continue

        # This is a liquidate() call to Morpho
        tx_from = tx.get("from", "").lower()
        target_borrower = extract_borrower_from_calldata(input_data)

        if target_borrower and target_borrower.lower() in borrower_set:
            targeted_borrowers.add(target_borrower.lower())
            competing_senders.add(tx_from)

            # Find the original address casing
            for addr in borrower_addresses:
                if addr.lower() == target_borrower.lower():
                    is_known_bot = tx_from in KNOWN_MEV_BOTS
                    results[addr] = HIGH
                    logger.info(
                        f"COMPETITION: {addr[:10]}... targeted by "
                        f"{'KNOWN MEV BOT' if is_known_bot else 'unknown sender'} "
                        f"{tx_from[:10]}... in tx {tx.get('hash', '?')[:18]}... "
                        f"(block {tx.get('blockNumber', '?')})"
                    )
                    break

    # --- Approach 2: Spot-check known MEV bots for recent Morpho activity ---
    # Only check bots we haven't already seen in approach 1, and limit to 3
    # to stay within rate limits (each is 1 API call).
    unchecked_bots = [b for b in KNOWN_MEV_BOTS if b not in competing_senders][:3]

    for bot_addr in unchecked_bots:
        bot_txs = get_recent_txs_from_address(bot_addr, from_block)

        for tx in bot_txs:
            input_data = tx.get("input", "")
            target_borrower = extract_borrower_from_calldata(input_data)

            if target_borrower and target_borrower.lower() in borrower_set:
                for addr in borrower_addresses:
                    if addr.lower() == target_borrower.lower() and results[addr] != HIGH:
                        results[addr] = HIGH
                        logger.info(
                            f"COMPETITION: {addr[:10]}... targeted by known MEV bot "
                            f"{bot_addr[:10]}... in tx {tx.get('hash', '?')[:18]}..."
                        )
                        break

    high_count = sum(1 for v in results.values() if v == HIGH)
    if high_count > 0:
        logger.info(
            f"Competition check: {high_count}/{len(borrower_addresses)} positions "
            f"flagged HIGH_COMPETITION"
        )
    else:
        logger.info(
            f"Competition check: all {len(borrower_addresses)} positions LOW_COMPETITION"
        )

    return results
