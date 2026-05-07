"""One-off script to look up full borrower address for wstUSR/USDC position."""
import requests

MORPHO_API_URL = "https://blue-api.morpho.org/graphql"

# The market ID starts with 0xd9e34b1eed46d123 based on the log
query = """
query {
    marketPositions(
        where: { marketUniqueKey_in: ["0xd9e34b1eed46d123a90de4f3c08e9a4683aaedb24a43e3e05d8f5b2dbfe46e51"], borrowShares_gte: "1" }
        first: 100
    ) {
        items {
            user { address }
            state { borrowAssets borrowShares collateral }
        }
    }
}
"""

resp = requests.post(
    MORPHO_API_URL,
    json={"query": query},
    headers={"Content-Type": "application/json"},
    timeout=30,
)
data = resp.json()
items = data.get("data", {}).get("marketPositions", {}).get("items", [])

print(f"Found {len(items)} borrowers")
for item in items:
    addr = item["user"]["address"]
    borrow = item["state"]["borrowAssets"]
    print(f"  {addr}  borrowAssets={borrow}")
    if addr.lower().startswith("0xe08d97e1"):
        print(f"\n>>> MATCH: {addr}")
