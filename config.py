"""
Configuration for Morpho Blue Liquidation Bot.
ABIs, addresses, constants, and tunable parameters.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- RPC ---
ALCHEMY_RPC_URL = os.environ.get("ALCHEMY_RPC_URL")
if not ALCHEMY_RPC_URL:
    raise EnvironmentError("ALCHEMY_RPC_URL environment variable is required")

# --- Contract Addresses ---
MORPHO_BLUE_ADDRESS = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
UNISWAP_SWAP_ROUTER02_ADDRESS = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
UNISWAP_QUOTER_V2_ADDRESS = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
EXECUTOR_ADDRESS = os.environ.get("EXECUTOR_ADDRESS")

# --- Morpho Blue Deployment Block (Ethereum Mainnet) ---
MORPHO_DEPLOYMENT_BLOCK = 18883124

# --- Morpho API ---
MORPHO_API_URL = "https://blue-api.morpho.org/graphql"

# --- Scan Parameters ---
SCAN_INTERVAL_SECONDS = 30
MAX_POSITIONS_PER_QUERY = 1000

# --- Execution Parameters ---
DRY_RUN = True  # Read-only mode: log opportunities, don't execute
MAX_GAS_PRICE_GWEI = 50
MIN_PROFIT_USD = 50
LIQUIDATION_GAS_ESTIMATE = 900_000
SLIPPAGE_BPS = 100
DEFAULT_UNISWAP_FEE_TIERS = [100, 500, 3000, 10000]

# Loan-token symbols treated as $1-pegged for the MIN_PROFIT_USD conversion.
STABLE_LOAN_TOKEN_SYMBOLS = {"USDC", "USDT", "DAI", "USDS", "USD0", "USDCV", "ZCHF"}

# Static USD price used for gas-cost estimates and the WETH min-profit conversion.
# Update manually until an oracle feed is wired in.
ETH_PRICE_USD = 1800.0

# Non-stable loan tokens supported by the v1 executor routing.
# Maps symbol (uppercase) -> static USD price. Keep separate from the stable set
# so MIN_PROFIT_USD is converted via price, not as if 1 token == $1.
LOAN_TOKEN_PRICES_USD: dict[str, float] = {
    "WETH": ETH_PRICE_USD,
}

# Python-side collateral allowlist — only attempt quotes/simulation for these tokens.
# Must match what is allowlisted on the deployed executor contract.
ALLOWED_COLLATERAL_TOKENS: set[str] = {
    "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0",  # wstETH
}

# Loaded from .env only. Required only when DRY_RUN is False.
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
BOT_WALLET_ADDRESS = os.environ.get("BOT_WALLET_ADDRESS")

# --- Market Blacklist ---
# Market IDs (hex prefixes) to skip entirely — e.g. illiquid collateral tokens
MARKET_BLACKLIST: set[str] = {
    # RLP/USDC — Resolv LP token, ~$3k daily DEX volume, slippage would eat the bonus
    "0xe1b65304edd8ceaea9b629df4c3c926a37d1216e27900505c04f14b2ed279f33",
}

# Minimum borrow-side USD value for a market to be worth scanning
MIN_MARKET_BORROW_USD = 1_000

# --- Stale Position Detection ---
# If a market's lastUpdate is older than this many blocks AND the position's LTV
# exceeds LLTV by this margin, flag it as likely already liquidated by another bot.
STALE_BLOCKS_THRESHOLD = 50       # ~10 minutes at 12s/block
STALE_LTV_EXCESS_THRESHOLD = 0.05  # LTV exceeds LLTV by more than 5%
MAX_SUBGRAPH_LAG_BLOCKS = 50

# --- Cross-Check ---
CROSS_CHECK_TOP_N = 5  # Number of top candidates to verify on-chain

# --- Etherscan API (for MEV competition check) ---
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
ETHERSCAN_API_URL = "https://api.etherscan.io/api"

# --- MEV Competition Detection ---
COMPETITION_CHECK_TOP_N = 20       # Check competition for top N profitable candidates
COMPETITION_LOOKBACK_BLOCKS = 50   # How far back to scan for competing liquidation txs (~10 min)

# Known MEV / liquidation bot addresses (lowercase for comparison).
# Sources: Etherscan labels, Flashbots research, public MEV bot databases.
KNOWN_MEV_BOTS: set[str] = {
    # Wintermute
    "0x00000000ae347930bd1e7b0f35588b92280f9e75",
    # MEV Bot: 0x000...86f (high-volume Morpho liquidator seen on-chain)
    "0x00000000003b3cc22af3ae1eac0440bcee416b40",
    # Aave/Morpho liquidation bot cluster
    "0x0000000000007f150bd6f54c40a34d7c3d5e9f56",
    # jaredfromsubway.eth (sandwich + liquidation)
    "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13",
    # MEV Bot: labeled "Morpho Liquidator 1" on Etherscan
    "0x6f1cdbbb4d53d226cf4b917bf768b94acbab6168",
    # Flashbots builder relay bot
    "0xdafea492d9c6733ae3d56b7ed1adb60692c98bc5",
}

# liquidate() function selector (first 4 bytes of keccak256)
LIQUIDATE_SELECTOR = "0xa4db47c3"

# --- Math Constants ---
WAD = 10**18
VIRTUAL_SHARES = 10**6
VIRTUAL_ASSETS = 1
ORACLE_PRICE_SCALE = 10**36

# --- LIF Constants ---
LIF_MAX = 115 * WAD // 100  # 1.15 in WAD
LIF_BETA = 3 * 10**17       # 0.3 in WAD
LIF_ONE_MINUS_BETA = 7 * 10**17  # 0.7 in WAD

# --- Minimal ABIs (IMorphoStaticTyping variant for view functions) ---

MORPHO_ABI = [
    # position(bytes32 id, address user) -> (uint256 supplyShares, uint128 borrowShares, uint128 collateral)
    {
        "inputs": [
            {"name": "id", "type": "bytes32"},
            {"name": "user", "type": "address"}
        ],
        "name": "position",
        "outputs": [
            {"name": "supplyShares", "type": "uint256"},
            {"name": "borrowShares", "type": "uint128"},
            {"name": "collateral", "type": "uint128"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    # market(bytes32 id) -> (uint128, uint128, uint128, uint128, uint128, uint128)
    {
        "inputs": [
            {"name": "id", "type": "bytes32"}
        ],
        "name": "market",
        "outputs": [
            {"name": "totalSupplyAssets", "type": "uint128"},
            {"name": "totalSupplyShares", "type": "uint128"},
            {"name": "totalBorrowAssets", "type": "uint128"},
            {"name": "totalBorrowShares", "type": "uint128"},
            {"name": "lastUpdate", "type": "uint128"},
            {"name": "fee", "type": "uint128"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    # idToMarketParams(bytes32 id) -> (address, address, address, address, uint256)
    {
        "inputs": [
            {"name": "id", "type": "bytes32"}
        ],
        "name": "idToMarketParams",
        "outputs": [
            {"name": "loanToken", "type": "address"},
            {"name": "collateralToken", "type": "address"},
            {"name": "oracle", "type": "address"},
            {"name": "irm", "type": "address"},
            {"name": "lltv", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    # liquidate(MarketParams, address, uint256, uint256, bytes) -> (uint256, uint256)
    {
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": [
                    {"name": "loanToken", "type": "address"},
                    {"name": "collateralToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "irm", "type": "address"},
                    {"name": "lltv", "type": "uint256"}
                ]
            },
            {"name": "borrower", "type": "address"},
            {"name": "seizedAssets", "type": "uint256"},
            {"name": "repaidShares", "type": "uint256"},
            {"name": "data", "type": "bytes"}
        ],
        "name": "liquidate",
        "outputs": [
            {"name": "", "type": "uint256"},
            {"name": "", "type": "uint256"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    # CreateMarket event
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "id", "type": "bytes32"},
            {
                "indexed": False,
                "name": "marketParams",
                "type": "tuple",
                "components": [
                    {"name": "loanToken", "type": "address"},
                    {"name": "collateralToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "irm", "type": "address"},
                    {"name": "lltv", "type": "uint256"}
                ]
            }
        ],
        "name": "CreateMarket",
        "type": "event"
    },
    # Borrow event
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "id", "type": "bytes32"},
            {"indexed": True, "name": "caller", "type": "address"},
            {"indexed": True, "name": "onBehalf", "type": "address"},
            {"indexed": False, "name": "receiver", "type": "address"},
            {"indexed": False, "name": "assets", "type": "uint256"},
            {"indexed": False, "name": "shares", "type": "uint256"}
        ],
        "name": "Borrow",
        "type": "event"
    },
]

ORACLE_ABI = [
    {
        "inputs": [],
        "name": "price",
        "outputs": [
            {"name": "", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

IRM_ABI = [
    # borrowRateView(MarketParams, Market) -> uint256
    {
        "inputs": [
            {
                "name": "marketParams",
                "type": "tuple",
                "components": [
                    {"name": "loanToken", "type": "address"},
                    {"name": "collateralToken", "type": "address"},
                    {"name": "oracle", "type": "address"},
                    {"name": "irm", "type": "address"},
                    {"name": "lltv", "type": "uint256"}
                ]
            },
            {
                "name": "market",
                "type": "tuple",
                "components": [
                    {"name": "totalSupplyAssets", "type": "uint128"},
                    {"name": "totalSupplyShares", "type": "uint128"},
                    {"name": "totalBorrowAssets", "type": "uint128"},
                    {"name": "totalBorrowShares", "type": "uint128"},
                    {"name": "lastUpdate", "type": "uint128"},
                    {"name": "fee", "type": "uint128"}
                ]
            }
        ],
        "name": "borrowRateView",
        "outputs": [
            {"name": "", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"}
                ]
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"}
                ]
            }
        ],
        "stateMutability": "payable",
        "type": "function"
    }
]

ERC20_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    }
]

EXECUTOR_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {
                        "components": [
                            {"name": "loanToken", "type": "address"},
                            {"name": "collateralToken", "type": "address"},
                            {"name": "oracle", "type": "address"},
                            {"name": "irm", "type": "address"},
                            {"name": "lltv", "type": "uint256"},
                        ],
                        "name": "marketParams",
                        "type": "tuple",
                    },
                    {"name": "borrower", "type": "address"},
                    {"name": "repaidShares", "type": "uint256"},
                    {"name": "uniswapFee", "type": "uint24"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "minProfit", "type": "uint256"},
                ],
                "name": "p",
                "type": "tuple",
            }
        ],
        "name": "executeLiquidation",
        "outputs": [
            {"name": "seizedAssets", "type": "uint256"},
            {"name": "repaidAssets", "type": "uint256"},
            {"name": "profit", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "allowed", "type": "bool"},
        ],
        "name": "setCollateralAllowed",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "fee", "type": "uint24"},
            {"name": "allowed", "type": "bool"},
        ],
        "name": "setFeeTierAllowed",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

QUOTER_V2_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]
