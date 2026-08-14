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


async def cleanup(session, label):
    """Cancel every open order and close every position. Leaves a clean slate."""
    code, orders = await show(session, "GET", "/v2/orders",
                              f"open orders ({label})", None)
    if isinstance(orders, list):
        print(f"    {len(orders)} open order(s)")
        for o in orders:
            print(f"      {o.get('symbol')} {o.get('side')} {o.get('type')} "
                  f"qty={o.get('qty')} status={o.get('status')} id={o.get('id')}")
            await show(session, "DELETE", f"/v2/orders/{o['id']}",
                       f"cancel {o.get('symbol')}")
    code, positions = await show(session, "GET", "/v2/positions",
                                 f"positions ({label})")
    if isinstance(positions, list):
        print(f"    {len(positions)} position(s)")
        for p in positions:
            print(f"      {p.get('symbol')} qty={p.get('qty')} "
                  f"pl={p.get('unrealized_pl')}")
            await show(session, "DELETE", f"/v2/positions/{p['symbol']}",
                       f"close {p.get('symbol')}")


async def sequence(symbol, qty):
    """Reproduce the bot's _enter exactly: market buy, then a stop sell."""
    async with aiohttp.ClientSession() as session:
        print("=" * 62)
        print("A. STATE BEFORE (leftover orders are a wash-trade trigger)")
        print("=" * 62)
        await cleanup(session, "before")
        await asyncio.sleep(3)

        print()
        print("=" * 62)
        print(f"B. REPRODUCE _enter: buy {qty} {symbol}, then stop-sell {qty}")
        print("=" * 62)
        code, order = await show(session, "POST", "/v2/orders", "1) market buy",
                                 {"symbol": symbol, "qty": str(qty),
                                  "side": "buy", "type": "market",
                                  "time_in_force": "day"})
        if code < 300:
            print("    buy ACCEPTED")
        stop_price = None
        code2, snap = await show(session, "GET", f"/v2/positions/{symbol}",
                                 "position after buy")
        if code2 == 200 and isinstance(snap, dict):
            stop_price = round(float(snap["avg_entry_price"]) * 0.97, 2)
            print(f"    filled avg={snap['avg_entry_price']} qty={snap['qty']}")
        stop_price = stop_price or 1.0
        code3, _ = await show(session, "POST", "/v2/orders",
                              "2) stop sell (the call that fails)",
                              {"symbol": symbol, "qty": str(qty),
                               "side": "sell", "type": "stop",
                               "time_in_force": "day",
                               "stop_price": f"{stop_price:.2f}"})
        print(f"    >>> stop-sell result: HTTP {code3}"
              f"{' OK' if code3 < 300 else '  <-- REPRODUCED THE FAILURE'}")

        print()
        print("=" * 62)
        print("C. ALTERNATIVE: single atomic OTO order (buy + attached stop)")
        print("=" * 62)
        await cleanup(session, "reset")
        await asyncio.sleep(3)
        code4, oto = await show(session, "POST", "/v2/orders", "oto buy+stop",
                                {"symbol": symbol, "qty": str(qty),
                                 "side": "buy", "type": "market",
                                 "time_in_force": "day",
                                 "order_class": "oto",
                                 "stop_loss": {"stop_price": f"{stop_price:.2f}"}})
        print(f"    >>> OTO result: HTTP {code4}"
              f"{'  <-- THE FIX WORKS' if code4 < 300 else ' FAILED'}")
        if code4 < 300 and isinstance(oto, dict):
            print(f"    parent id={oto.get('id')} legs="
                  f"{[(l.get('type'), l.get('id')) for l in (oto.get('legs') or [])]}")

        print()
        print("=" * 62)
        print("D. CLEANUP")
        print("=" * 62)
        await asyncio.sleep(3)
        await cleanup(session, "after")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", nargs="*", default=["AAPL"],
                        help="symbols to inspect and test-order")
    parser.add_argument("--qty", type=int, default=1,
                        help="share quantity for the probe order")
    parser.add_argument("--sequence", metavar="SYMBOL",
                        help="reproduce the bot's buy-then-stop entry and "
                             "test the atomic OTO alternative, then clean up")
    args = parser.parse_args()
    if args.sequence:
        asyncio.run(sequence(args.sequence, args.qty))
    else:
        asyncio.run(main(args.probe, args.qty))
