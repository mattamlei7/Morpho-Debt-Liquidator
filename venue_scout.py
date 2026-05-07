"""
Read-only venue scout for current Morpho liquidation candidates.

Loads the latest candidate_classifier.jsonl cycle, asks configured external
venues for collateral -> loan quotes, and checks whether any quote clears:

    required_repay + production min_profit

No transactions are signed or broadcast.
"""

import argparse
import json
import logging
import os
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from config import BOT_WALLET_ADDRESS, EXECUTOR_ADDRESS, SLIPPAGE_BPS

log = logging.getLogger("venue_scout")

ZEROX_PRICE_URL = "https://api.0x.org/swap/allowance-holder/price"
ONEINCH_QUOTE_URL = "https://api.1inch.com/swap/v6.1/1/quote"
DEFAULT_TAKER = "0x0000000000000000000000000000000000000001"


@dataclass
class VenueResult:
    venue: str
    status: str
    buy_amount: Optional[str] = None
    buy_amount_after_slippage: Optional[str] = None
    clears_repay: Optional[bool] = None
    clears_min_profit: Optional[bool] = None
    surplus: Optional[str] = None
    error: Optional[str] = None
    sources: Optional[str] = None


def load_latest_cycle(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    if not rows:
        return []

    latest_ts = rows[-1]["ts"]
    return [row for row in rows if row.get("ts") == latest_ts]


def candidate_target(row: dict[str, Any]) -> Optional[int]:
    try:
        return int(row["required_repay"]) + int(row["min_profit"])
    except (KeyError, TypeError, ValueError):
        return None


def evaluate_quote(venue: str, buy_amount: int, row: dict[str, Any], sources: str = "") -> VenueResult:
    target = candidate_target(row)
    if target is None:
        return VenueResult(venue=venue, status="missing_target", error="required_repay/min_profit missing")

    required_repay = int(row["required_repay"])
    after_slippage = buy_amount * (10_000 - SLIPPAGE_BPS) // 10_000
    surplus = after_slippage - required_repay

    return VenueResult(
        venue=venue,
        status="ok",
        buy_amount=str(buy_amount),
        buy_amount_after_slippage=str(after_slippage),
        clears_repay=after_slippage >= required_repay,
        clears_min_profit=after_slippage >= target,
        surplus=str(surplus),
        sources=sources or None,
    )


def error_result(venue: str, status: str, error: str) -> VenueResult:
    return VenueResult(venue=venue, status=status, error=error[:500])


def scout_0x(row: dict[str, Any], taker: str, timeout: int) -> VenueResult:
    api_key = os.environ.get("ZEROX_API_KEY") or os.environ.get("0X_API_KEY")
    if not api_key:
        return error_result("0x", "missing_api_key", "Set ZEROX_API_KEY to enable 0x Swap API quotes")

    params = {
        "chainId": "1",
        "sellToken": row["collateral_token"],
        "buyToken": row["loan_token"],
        "sellAmount": row["estimated_seized_collateral"],
        "taker": taker,
    }
    headers = {
        "0x-api-key": api_key,
        "0x-version": "v2",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(ZEROX_PRICE_URL, params=params, headers=headers, timeout=timeout)
        data = resp.json()
    except Exception as e:
        return error_result("0x", "request_error", f"{type(e).__name__}: {e}")

    if resp.status_code >= 400:
        return error_result("0x", f"http_{resp.status_code}", json.dumps(data)[:500])

    buy_amount = data.get("buyAmount")
    if buy_amount is None:
        return error_result("0x", "no_quote", json.dumps(data)[:500])

    route = data.get("route") or {}
    fills = route.get("fills") or []
    sources = ",".join(sorted({str(fill.get("source")) for fill in fills if fill.get("source")}))
    return evaluate_quote("0x", int(buy_amount), row, sources=sources)


def scout_1inch(row: dict[str, Any], timeout: int) -> VenueResult:
    api_key = os.environ.get("ONEINCH_API_KEY") or os.environ.get("ONE_INCH_API_KEY")
    if not api_key:
        return error_result("1inch", "missing_api_key", "Set ONEINCH_API_KEY to enable 1inch quotes")

    params = {
        "src": row["collateral_token"],
        "dst": row["loan_token"],
        "amount": row["estimated_seized_collateral"],
        "includeProtocols": "true",
        "includeGas": "true",
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        resp = requests.get(ONEINCH_QUOTE_URL, params=params, headers=headers, timeout=timeout)
        data = resp.json()
    except Exception as e:
        return error_result("1inch", "request_error", f"{type(e).__name__}: {e}")

    if resp.status_code >= 400:
        return error_result("1inch", f"http_{resp.status_code}", json.dumps(data)[:500])

    buy_amount = data.get("dstAmount")
    if buy_amount is None:
        return error_result("1inch", "no_quote", json.dumps(data)[:500])

    sources = []
    for route in data.get("protocols") or []:
        for hop in route.get("hops") or []:
            for protocol in hop.get("protocols") or []:
                name = protocol.get("name")
                if name:
                    sources.append(name)

    return evaluate_quote("1inch", int(buy_amount), row, sources=",".join(sorted(set(sources))))


def scout_pendle(row: dict[str, Any]) -> VenueResult:
    del row
    return error_result(
        "pendle",
        "not_configured",
        "No token->Pendle-market mapping configured yet; add explicit PT/SY markets before quoting",
    )


def scout_native(row: dict[str, Any]) -> VenueResult:
    del row
    return error_result(
        "native_redemption",
        "not_configured",
        "No token-specific redemption adapter configured; requires explicit protocol integration per collateral",
    )


def write_reports(records: list[dict[str, Any]], jsonl_path: Path, md_path: Path) -> None:
    with jsonl_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    actionable = [rec for rec in records if any(v.get("clears_min_profit") for v in rec["venues"])]
    counts = Counter(v["status"] for rec in records for v in rec["venues"])

    lines = [
        f"# Venue scout report ({datetime.now(timezone.utc).isoformat(timespec='seconds')})",
        "",
        f"Rows: `{len(records)}`. Actionable by quote: `{len(actionable)}`.",
        "",
        "| Pair | Category | Oracle bonus $ | Best venue | Best surplus | Clears min profit | Notes |",
        "|---|---|---:|---|---:|:---:|---|",
    ]

    for rec in records:
        ok_venues = [v for v in rec["venues"] if v.get("status") == "ok"]
        best = max(ok_venues, key=lambda v: int(v.get("surplus") or "-999999999999999999999999999"), default=None)
        if best:
            best_venue = best["venue"]
            best_surplus = best["surplus"]
            clears = "Y" if best.get("clears_min_profit") else "N"
            notes = best.get("sources") or ""
        else:
            best_venue = "-"
            best_surplus = ""
            clears = "N"
            notes = "; ".join(f"{v['venue']}:{v['status']}" for v in rec["venues"])

        lines.append(
            f"| {rec['pair']} | {rec['category']} | {rec.get('oracle_bonus_usd') or 0:.2f} | "
            f"{best_venue} | {best_surplus} | {clears} | {notes} |"
        )

    lines.extend(["", "Status counts:"])
    for status, count in sorted(counts.items()):
        lines.append(f"- `{status}`: {count}")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only external venue scout")
    parser.add_argument("--input", default="candidate_classifier.jsonl")
    parser.add_argument("--jsonl", default="venue_scout.jsonl")
    parser.add_argument("--md", default="venue_scout.md")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--include-profitable", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    rows = load_latest_cycle(Path(args.input))
    if not rows:
        log.error("No classifier rows found in %s", args.input)
        return 1

    if not args.include_profitable:
        rows = [row for row in rows if row.get("category") != "executable_profitable"]

    taker = os.environ.get("VENUE_SCOUT_TAKER") or EXECUTOR_ADDRESS or BOT_WALLET_ADDRESS or DEFAULT_TAKER
    records = []
    for idx, row in enumerate(rows, 1):
        pair = f"{row.get('collateral_symbol')}/{row.get('loan_symbol')}"
        log.info("%s/%s %s %s", idx, len(rows), pair, row.get("category"))

        venues = [
            scout_0x(row, taker, args.timeout),
            scout_1inch(row, args.timeout),
            scout_pendle(row),
            scout_native(row),
        ]
        records.append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pair": pair,
            "category": row.get("category"),
            "market_id": row.get("market_id"),
            "borrower": row.get("borrower"),
            "collateral_token": row.get("collateral_token"),
            "loan_token": row.get("loan_token"),
            "estimated_seized_collateral": row.get("estimated_seized_collateral"),
            "required_repay": row.get("required_repay"),
            "min_profit": row.get("min_profit"),
            "oracle_bonus_usd": row.get("oracle_bonus_usd"),
            "venues": [asdict(v) for v in venues],
        })

    write_reports(records, Path(args.jsonl), Path(args.md))
    actionable = sum(1 for rec in records if any(v.get("clears_min_profit") for v in rec["venues"]))
    log.info("Wrote %s and %s; actionable_by_quote=%s", args.jsonl, args.md, actionable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
