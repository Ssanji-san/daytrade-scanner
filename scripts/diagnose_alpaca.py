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
            # Which day-trading regime is this account on? The old rule
            # ("under $25k = 3 day trades per 5 business days") was replaced
            # around mid-2026 by real-time margin exposure, and brokers have
            # until October 2027 to implement it - so report what the account
            # actually exposes rather than inferring a rule from equity.
            if "pattern_day_trader" not in acct and "daytrade_count" not in acct:
                print("    NOTE: no pattern_day_trader / daytrade_count field "
                      "-> this account is not on the old PDT count regime.")
                print("          Buying power is the binding constraint; see "
                      "buying_power above.")
            elif acct.get("pattern_day_trader"):
                print("    !! FLAGGED PATTERN DAY TRADER: the broker's own "
                      "daytrading_buying_power is what limits entries.")
                print(f"       daytrade_count = {acct.get('daytrade_count')}")

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


async def live_entry(symbol, qty):
    """Exercise the REAL Broker code path against the paper API, then undo it."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from scanner.config import DEFAULT
    from scanner.trading.broker import Broker
    from scanner.trading.strategy import exit_levels

    async with aiohttp.ClientSession() as session:
        await cleanup(session, "before")
        await asyncio.sleep(3)
        broker = Broker(session, DEFAULT)       # PaperOnlyError if misconfigured
        print(f"\n>>> Broker.submit_oto_stop({symbol}, {qty}) "
              "- the real bot entry path")
        levels = None
        try:
            snap = await broker._request(
                "GET", "/v2/positions")   # cheap auth check
            price_resp = await session.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                headers=broker.headers)
            price = (await price_resp.json())["trade"]["p"]
            levels = exit_levels(price, DEFAULT)
            print(f"    last={price} stop={levels['stop']:.2f} "
                  f"scale_out={levels['scale_out']:.2f}")
            order = await broker.submit_oto_stop(symbol, qty, levels["stop"])
            print(f"    *** ENTRY ACCEPTED *** id={order.get('id')} "
                  f"status={order.get('status')}")
            print(f"    legs={[(l.get('type'), l.get('status')) for l in (order.get('legs') or [])]}")
        except Exception as exc:
            print(f"    !!! ENTRY FAILED: {type(exc).__name__}: {exc}")
        await asyncio.sleep(5)
        print("\n>>> Broker.cancel_orders_for + close (undo the test trade)")
        try:
            await broker.cancel_orders_for(symbol)
            await asyncio.sleep(2)
            await broker.close_position(symbol)
            print("    cleaned up via the bot's own exit path")
        except Exception as exc:
            print(f"    cleanup note: {type(exc).__name__}: {exc}")
        await asyncio.sleep(3)
        await cleanup(session, "after")


async def history(fills_date=None):
    """Where did the account balance actually come from? Fills vs deposits."""
    async with aiohttp.ClientSession() as session:
        if fills_date:
            print("=" * 62)
            print(f"EVERY FILL ON {fills_date}")
            print("=" * 62)
            code, day = await show(session, "GET",
                                   f"/v2/account/activities/FILL?date={fills_date}"
                                   "&page_size=100", f"fills {fills_date}")
            if isinstance(day, list):
                print(f"    {len(day)} fill(s)")
                for a in day:
                    print(f"      {a.get('transaction_time','')[11:19]} "
                          f"{a.get('symbol'):6} {a.get('side'):10} "
                          f"qty={a.get('qty'):>6} @ {a.get('price')}")
            return
        print("=" * 62)
        print("ACCOUNT ACTIVITIES (fills, deposits, transfers)")
        print("=" * 62)
        code, acts = await show(session, "GET", "/v2/account/activities",
                                "activities")
        if isinstance(acts, list):
            print(f"    {len(acts)} activity record(s)")
            fills = [a for a in acts if a.get("activity_type") == "FILL"]
            other = [a for a in acts if a.get("activity_type") != "FILL"]
            print(f"    FILL records: {len(fills)}   non-FILL: {len(other)}")
            print("    --- non-FILL (deposits/resets/journals) ---")
            for a in other[:20]:
                print(f"      {a.get('date') or a.get('transaction_time')} "
                      f"{a.get('activity_type')} net={a.get('net_amount')} "
                      f"desc={a.get('description')}")
            print("    --- fills (oldest 25) ---")
            for a in sorted(fills, key=lambda x: x.get("transaction_time") or "")[:25]:
                print(f"      {a.get('transaction_time')} {a.get('symbol')} "
                      f"{a.get('side')} qty={a.get('qty')} @ {a.get('price')}")
        print()
        print("=" * 62)
        print("EQUITY BY DAY (what the dashboard chart plots)")
        print("=" * 62)
        code, hist = await show(session, "GET",
                                "/v2/account/portfolio/history"
                                "?period=1M&timeframe=1D", "portfolio history")
        if isinstance(hist, dict):
            import datetime as _dt
            ts = hist.get("timestamp") or []
            eq = hist.get("equity") or []
            pl = hist.get("profit_loss") or []
            for i, t in enumerate(ts):
                d = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%a %m-%d")
                e = eq[i] if i < len(eq) else None
                p = pl[i] if i < len(pl) else None
                if e is not None:
                    print(f"      {d}  equity=${e:>10,.2f}  day P/L={p}")


async def bars(symbol):
    """Read-only: was this gapper actually tradable, and at what size?"""
    import datetime as _dt
    async with aiohttp.ClientSession() as session:
        print("=" * 62)
        print(f"ASSET {symbol}")
        print("=" * 62)
        await show(session, "GET", f"/v2/assets/{symbol}", f"asset {symbol}")

        data = "https://data.alpaca.markets"
        start = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
        print()
        print("=" * 62)
        print("DAILY BARS (o/h/l/c and volume)")
        print("=" * 62)
        async with session.get(
                f"{data}/v2/stocks/{symbol}/bars",
                params={"timeframe": "1Day", "start": start, "feed": "iex"},
                headers=HEADERS) as resp:
            payload = await resp.json()
        for b in (payload.get("bars") or []):
            print(f"   {b['t'][:10]}  o={b['o']:<9} h={b['h']:<9} "
                  f"l={b['l']:<9} c={b['c']:<9} vol={b['v']:,}")

        print()
        print("=" * 62)
        print("MINUTE BARS AROUND THE OPEN (is there size to trade?)")
        print("=" * 62)
        today = _dt.date.today().isoformat()
        async with session.get(
                f"{data}/v2/stocks/{symbol}/bars",
                params={"timeframe": "1Min", "start": today,
                        "limit": 400, "feed": "iex"},
                headers=HEADERS) as resp:
            payload = await resp.json()
        rows = payload.get("bars") or []
        print(f"   {len(rows)} minute bars on the IEX feed today")
        for b in rows[:12]:
            print(f"   {b['t'][11:16]}Z  o={b['o']:<9} h={b['h']:<9} "
                  f"l={b['l']:<9} c={b['c']:<9} vol={b['v']:,}")


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
    parser.add_argument("--fills-date", default=None,
                        help="list every fill on this YYYY-MM-DD")
    parser.add_argument("--bars", default=None,
                        help="read-only daily+minute bars for a symbol")
    parser.add_argument("--history", action="store_true",
                        help="show account activities + equity by day")
    parser.add_argument("--live-entry", metavar="SYMBOL",
                        help="run the real Broker entry path on this symbol, "
                             "then cancel/close it again")
    parser.add_argument("--sequence", metavar="SYMBOL",
                        help="reproduce the bot's buy-then-stop entry and "
                             "test the atomic OTO alternative, then clean up")
    args = parser.parse_args()
    if args.bars:
        asyncio.run(bars(args.bars))
    elif args.history:
        asyncio.run(history(args.fills_date))
    elif args.live_entry:
        asyncio.run(live_entry(args.live_entry, args.qty))
    elif args.sequence:
        asyncio.run(sequence(args.sequence, args.qty))
    else:
        asyncio.run(main(args.probe, args.qty))
