"""End-of-day safety net: cancel open orders, close positions, reconcile.

    python -m scanner.flatten [--force]

Run by GitHub Actions near 15:50 ET. Normally there is nothing to do -
the bot's time stop closed stragglers during the session and bracket
legs handled the rest on Alpaca's servers. Steps:

1. Reconcile: journal trades with no exit whose position is gone were
   closed by brackets after the session ended - record their fills.
2. If within the flatten window (>= 15:40 ET, market still open) or
   --force: cancel all open orders and close all remaining positions.

Idempotent; safe to run twice (two cron times cover DST drift).
"""
import argparse
import asyncio
import datetime as dt

import aiohttp

from .config import DEFAULT
from .trading.broker import Broker
from .trading.journal import Journal
from .trading.strategy import ET

FLATTEN_FROM = (15, 40)
MARKET_CLOSE = (16, 0)


async def reconcile(broker, journal, position_symbols, now_ts):
    """Record exits for journal trades whose position no longer exists."""
    for trade in journal.open_trade_rows():
        if trade["symbol"] in position_symbols:
            continue
        exit_price, qty = 0.0, 0.0
        orders = await broker._request(
            "GET", "/v2/orders",
            params={"status": "closed", "symbols": trade["symbol"],
                    "limit": 20, "nested": "true"})
        for order in orders or []:
            legs = [order] + (order.get("legs") or [])
            for leg in legs:
                if leg.get("side") == "sell" and leg.get("filled_avg_price"):
                    filled = float(leg.get("filled_qty") or 0)
                    qty += filled
                    exit_price += filled * float(leg["filled_avg_price"])
        price = exit_price / qty if qty else trade["entry"]
        journal.record_trade_close(trade["id"], now_ts, price, "bracket")
        print(f"[flatten] reconciled {trade['symbol']} exit ~{price:.2f}")


async def run(force=False):
    cfg = DEFAULT
    now_et = dt.datetime.now(ET)
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    in_window = FLATTEN_FROM <= (now_et.hour, now_et.minute) < MARKET_CLOSE

    async with aiohttp.ClientSession() as session:
        broker = Broker(session, cfg)
        positions = await broker.positions()
        journal = Journal(cfg.bot_journal_path)
        await reconcile(broker, journal, {p["symbol"] for p in positions},
                        now_ts)

        # An open order with no position behind it (an attached stop whose
        # position already closed) makes tomorrow's buy on that symbol look
        # like a wash trade, so clear those regardless of the time window.
        position_symbols = {p["symbol"] for p in positions}
        for order in await broker.open_orders():
            if order["symbol"] not in position_symbols:
                try:
                    await broker.cancel_order(order["id"])
                    print(f"[flatten] cancelled orphan order {order['symbol']}")
                except aiohttp.ClientResponseError:
                    pass

        if not positions:
            print("[flatten] no open positions")
            return
        if not (in_window or force):
            print(f"[flatten] {len(positions)} open position(s) but outside "
                  f"the flatten window ({now_et:%H:%M} ET) - leaving alone")
            return

        for order in await broker.open_orders():
            try:
                await broker.cancel_order(order["id"])
            except aiohttp.ClientResponseError:
                pass
        for pos in positions:
            symbol = pos["symbol"]
            await broker.close_position(symbol)
            print(f"[flatten] closed {symbol}")
        # record exits for journaled trades we just closed
        await asyncio.sleep(3)
        await reconcile(broker, journal, set(), now_ts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="flatten regardless of time of day")
    args = parser.parse_args()
    asyncio.run(run(force=args.force))
