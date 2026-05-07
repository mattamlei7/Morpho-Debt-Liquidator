"""
One-off script to look up full market params for the MC-USR/USD0 market
and the current block number for fork test pinning.
"""
import json
import os
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

RPC = os.getenv("ALCHEMY_RPC_URL")
w3 = Web3(Web3.HTTPProvider(RPC))

MORPHO = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

# Minimal ABI for idToMarketParams
ABI = [
    {
        "inputs": [{"name": "id", "type": "bytes32"}],
        "name": "idToMarketParams",
        "outputs": [
            {"name": "loanToken", "type": "address"},
            {"name": "collateralToken", "type": "address"},
            {"name": "oracle", "type": "address"},
            {"name": "irm", "type": "address"},
            {"name": "lltv", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "id", "type": "bytes32"}],
        "name": "market",
        "outputs": [
            {"name": "totalSupplyAssets", "type": "uint128"},
            {"name": "totalSupplyShares", "type": "uint128"},
            {"name": "totalBorrowAssets", "type": "uint128"},
            {"name": "totalBorrowShares", "type": "uint128"},
            {"name": "lastUpdate", "type": "uint128"},
            {"name": "fee", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "id", "type": "bytes32"},
            {"name": "user", "type": "address"},
        ],
        "name": "position",
        "outputs": [
            {"name": "supplyShares", "type": "uint256"},
            {"name": "borrowShares", "type": "uint128"},
            {"name": "collateral", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

morpho = w3.eth.contract(address=MORPHO, abi=ABI)

# Find the MC-USR/USD0 market - we know the truncated ID starts with 0xcc39b6c9
# Query the Morpho API for all markets and find the one matching
import requests

query = """
query {
    markets(where: { chainId_in: [1], borrowAssetsUsd_gte: 10 }) {
        items {
            uniqueKey
            lltv
            oracleAddress
            irmAddress
            loanAsset { address symbol }
            collateralAsset { address symbol }
            state { borrowAssetsUsd }
        }
    }
}
"""

resp = requests.post(
    "https://blue-api.morpho.org/graphql",
    json={"query": query},
    headers={"Content-Type": "application/json"},
    timeout=30,
)
data = resp.json()
items = data.get("data", {}).get("markets", {}).get("items", [])

# Find the wstETH/WETH market (0xc54d7acf...) - has Uniswap V3 liquidity
target = None
for item in items:
    if item["uniqueKey"].startswith("0xc54d7acf"):
        target = item
        break

if not target:
    for item in items:
        loan_sym = item.get("loanAsset", {}).get("symbol", "")
        coll_sym = item.get("collateralAsset", {}).get("symbol", "") if item.get("collateralAsset") else ""
        if "wstETH" in coll_sym and "WETH" in loan_sym:
            print(f"Found by symbol: {item['uniqueKey']}")
            target = item
            break

if target:
    print("=" * 60)
    print("MARKET FOUND")
    print("=" * 60)
    print(f"Market ID:        {target['uniqueKey']}")
    print(f"Loan Token:       {target['loanAsset']['address']} ({target['loanAsset']['symbol']})")
    coll = target.get('collateralAsset', {})
    print(f"Collateral Token: {coll.get('address', 'N/A')} ({coll.get('symbol', 'N/A')})")
    print(f"Oracle:           {target['oracleAddress']}")
    print(f"IRM:              {target['irmAddress']}")
    print(f"LLTV:             {target['lltv']}")
    print(f"Borrow USD:       {target['state']['borrowAssetsUsd']}")

    # Now get on-chain position data for the borrower
    market_id = bytes.fromhex(target['uniqueKey'].replace("0x", ""))
    borrower = "0x69b7126E34672F174Aa754D6B1ccAA3f2095f12a"

    pos = morpho.functions.position(market_id, borrower).call()
    print(f"\nBORROWER POSITION: {borrower}")
    print(f"Supply Shares:    {pos[0]}")
    print(f"Borrow Shares:    {pos[1]}")
    print(f"Collateral:       {pos[2]}")

    state = morpho.functions.market(market_id).call()
    print(f"\nMARKET STATE:")
    print(f"Total Supply Assets:  {state[0]}")
    print(f"Total Supply Shares:  {state[1]}")
    print(f"Total Borrow Assets:  {state[2]}")
    print(f"Total Borrow Shares:  {state[3]}")
    print(f"Last Update:          {state[4]}")
    print(f"Fee:                  {state[5]}")

    block = w3.eth.block_number
    print(f"\nCURRENT BLOCK: {block}")
    print(f"\n--- Solidity test constants ---")
    print(f'address internal constant BORROWER = {borrower};')
    print(f'address internal constant LOAN_TOKEN = {target["loanAsset"]["address"]};')
    print(f'address internal constant COLLATERAL_TOKEN = {coll.get("address", "N/A")};')
    print(f'address internal constant ORACLE = {target["oracleAddress"]};')
    print(f'address internal constant IRM = {target["irmAddress"]};')
    print(f'uint256 internal constant LLTV = {target["lltv"]};')
    print(f'uint256 internal constant FORK_BLOCK = {block};')
    print(f'uint256 internal constant BORROW_SHARES = {pos[1]};')
else:
    print("Market not found! Listing all markets with USR or USD0:")
    for item in items:
        loan_sym = item.get("loanAsset", {}).get("symbol", "")
        coll_sym = item.get("collateralAsset", {}).get("symbol", "") if item.get("collateralAsset") else ""
        if "USR" in loan_sym or "USR" in coll_sym or "USD0" in loan_sym or "USD0" in coll_sym:
            print(f"  {item['uniqueKey'][:20]}... {coll_sym}/{loan_sym} borrow=${item['state']['borrowAssetsUsd']}")
