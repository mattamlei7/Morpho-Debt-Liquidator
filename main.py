"""
Morpho Blue Liquidation Bot - Main Entry Point.
Connects to Ethereum, scans for liquidatable positions, logs opportunities.
"""

import logging
import sys
import time
from logging.handlers import RotatingFileHandler

from web3 import Web3

from config import (
    ALCHEMY_RPC_URL,
    MORPHO_BLUE_ADDRESS,
    MORPHO_ABI,
    SCAN_INTERVAL_SECONDS,
    DRY_RUN,
)
from scanner import scan_all_markets
from executor import process_candidates


def setup_logging() -> logging.Logger:
    """Configure logging with console and rotating file handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (INFO)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (DEBUG, rotating 10MB, 5 backups)
    file_handler = RotatingFileHandler(
        "liquidator.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    return logging.getLogger("morpho-liquidator")


def setup_web3() -> Web3:
    """Create and verify Web3 connection."""
    w3 = Web3(Web3.HTTPProvider(
        ALCHEMY_RPC_URL,
        request_kwargs={"timeout": 30},
    ))

    if not w3.is_connected():
        raise ConnectionError("Failed to connect to Ethereum node")

    return w3


def run_scan_loop(w3: Web3, morpho, logger: logging.Logger) -> None:
    """Main scan loop. Runs every SCAN_INTERVAL_SECONDS."""
    logger.info("Entering scan loop")

    while True:
        try:
            start_time = time.time()
            block_number = w3.eth.block_number
            logger.info(f"--- Scan starting at block {block_number} ---")

            # Scan all markets for liquidatable positions
            candidates = scan_all_markets(w3, morpho)

            if candidates:
                logger.info(f"Found {len(candidates)} liquidatable position(s)")
                process_candidates(w3, morpho, candidates, logger)
            else:
                logger.info("No liquidatable positions found")

            elapsed = time.time() - start_time
            logger.info(f"Scan completed in {elapsed:.1f}s")

            # Sleep for remaining interval
            sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Shutdown requested, exiting")
            break
        except Exception as e:
            logger.error(f"Scan loop error: {e}", exc_info=True)
            time.sleep(SCAN_INTERVAL_SECONDS)


def main():
    logger = setup_logging()

    logger.info("=" * 60)
    logger.info("Morpho Blue Liquidation Bot Starting")
    logger.info(f"Mode: {'READ-ONLY (DRY_RUN)' if DRY_RUN else 'LIVE EXECUTION'}")
    logger.info(f"Scan interval: {SCAN_INTERVAL_SECONDS}s")
    logger.info("=" * 60)

    # Connect to Ethereum
    try:
        w3 = setup_web3()
    except Exception as e:
        logger.critical(f"Failed to connect to Ethereum: {e}")
        sys.exit(1)

    chain_id = w3.eth.chain_id
    block_number = w3.eth.block_number
    logger.info(f"Connected to chain {chain_id} at block {block_number}")

    if chain_id != 1:
        logger.warning(f"Expected Ethereum mainnet (chain 1), got chain {chain_id}")

    # Create Morpho contract instance
    morpho = w3.eth.contract(
        address=Web3.to_checksum_address(MORPHO_BLUE_ADDRESS),
        abi=MORPHO_ABI,
    )
    logger.info(f"Morpho Blue contract: {MORPHO_BLUE_ADDRESS}")

    # Start scanning
    run_scan_loop(w3, morpho, logger)


if __name__ == "__main__":
    main()
