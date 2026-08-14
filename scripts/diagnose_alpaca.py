"""Ground-truth probe of the Alpaca paper account and order acceptance.

    python scripts/diagnose_alpaca.py [--probe SYMBOL [SYMBOL ...]]

Answers, with the real API response bodies (which the bot's normal error
path throws away):
  1. Account state - equity, buying power, PDT flag, day-trade count, blocks.
  2. Asset state   - is each symbol actually tradable / shortable / fractionable.
  3. Order probe   - submit a qty=1 market buy and print the FULL error body
                     on rejection, so a 422 explains itself. Any fill is
                     closed again immediately (paper account, no real money).

Run from GitHub Actions (diagnose workflow) where the keys live.
"""
import argparse
import asyncio
import json
import os

import aiohttp

BASE = "https://paper-api.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID": os.environ.get("ALPACA_KEY", ""),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET", ""),
}
ACCOUNT_FIELDS = [
    "status", "equity", "cash", "buying_power", "daytrading_buying_power",
    "regt_buying_power", "pattern_day_trader", "daytrade_count",
    "trading_blocked", "account_blocked", "trade_suspended_by_user",
    "shorting_enabled", "multiplier", "options_buying_power",
]


async def show(session, method, path, label, payload=None):
    """Request and print status + body. Returns (status, parsed_or_text)."""
    url = BASE + path
    async with session.request(method, url, headers=HEADERS,
                               json=payload) as resp:
        text = await resp.text()
        print(f"\n--- {label}: HTTP {resp.status} {method} {path}")
        try:
            data = json.loads(text)
        except ValueError:
            print(f"    body: {text[:400]}")
            return resp.status, text
        if resp.status >= 400:
            print(f"    ERROR BODY: {json.dumps(data)[:600]}")
        return resp.status, data


async def main(probe_symbols, qty=1):
    if not HEADERS["APCA-API-KEY-ID"]:
        print("!! ALPACA_KEY / ALPACA_SECRET not set in the environment")
        return
    async with aiohttp.ClientSession() as session:
        print("=" * 62)
        print("1. ACCOUNT")
        print("=" * 62)
        status, acct = await show(session, "GET", "/v2/account", "account")
        if isinstance(acct, dict):
            print(f"    (all keys: {sorted(acct.keys())})")
            for field in ACCOUNT_FIELDS:
                if field in acct:
                    print(f"    {field:26} = {acct[field]}")
            equity = float(acct.get("equity") or 0)
            if equity < 25_000 and not acct.get("pattern_day_trader"):
                print("    NOTE: equity < $25,000 -> PDT rule caps day trades "
                      "at 3 per 5 business days.")
            if acct.get("pattern_day_trader"):
                print("    !! FLAGGED PATTERN DAY TRADER: with equity < $25k "
                      "Alpaca will REJECT further day-trade orders.")

        print()
        print("=" * 62)
        print("2. ASSETS")
        print("=" * 62)
        for sym in probe_symbols:
            code, asset = await show(session, "GET", f"/v2/assets/{sym}",
                                     f"asset {sym}")
            if code == 200 and isinstance(asset, dict):
                print(f"    tradable={asset.get('tradable')} "
                      f"status={asset.get('status')} "
                      f"exchange={asset.get('exchange')} "
                      f"fractionable={asset.get('fractionable')} "
                      f"class={asset.get('class')}")

        print()
        print("=" * 62)
        print(f"3. ORDER PROBE (qty={qty} market buy, paper only)")
        print("=" * 62)
        for sym in probe_symbols:
            payload = {"symbol": sym, "qty": str(qty), "side": "buy",
                       "type": "market", "time_in_force": "day"}
            code, order = await show(session, "POST", "/v2/orders",
                                     f"probe buy {sym}", payload)
            if code < 300 and isinstance(order, dict):
                print(f"    ACCEPTED id={order.get('id')} -> closing it back")
                await asyncio.sleep(2)
                await show(session, "DELETE", f"/v2/positions/{sym}",
                           f"close {sym}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", nargs="*", default=["AAPL"],
                        help="symbols to inspect and test-order")
    parser.add_argument("--qty", type=int, default=1,
                        help="share quantity for the probe order")
    args = parser.parse_args()
    asyncio.run(main(args.probe, args.qty))
