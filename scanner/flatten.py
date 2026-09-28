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
from .trading.strategy import ET, MARKET_OPEN, is_premarket, weighted_exit

FLATTEN_FROM = (15, 40)
MARKET_CLOSE = (16, 0)
# Orders that actually stop a loss. A resting limit sell above the market
# does not, and it reserves shares a new stop would need.
PROTECTIVE = ("stop", "trailing_stop", "stop_limit")
CANCEL_SETTLE_SECONDS = 5.0


def _qty(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def plan_protection(positions, open_orders, stops, cfg):
    """What each open position needs so none is left without a stop.

    Pure. `stops` maps symbol -> the stop the journal recorded for its open
    trade; a position the journal does not know gets the configured stop
    below its average entry. Actions: "ok" (already covered by a broker
    stop), "stop" (place one for the uncovered shares, after cancelling any
    resting non-protective sells), "close" (already through the stop), and
    "short" (reported, never sold - a sell would only add to it).
    """
    plan = []
    for pos in positions:
        symbol = pos["symbol"]
        held = _qty(pos.get("qty"))
        if held < 0:
            plan.append({"symbol": symbol, "action": "short", "qty": held})
            continue
        if held == 0:
            continue
        sells = [o for o in open_orders
                 if o.get("symbol") == symbol and o.get("side") == "sell"]
        covered = sum(_qty(o.get("qty")) for o in sells
                      if o.get("type") in PROTECTIVE)
        if covered >= held:
            plan.append({"symbol": symbol, "action": "ok"})
            continue
        price = float(pos.get("current_price") or 0)
        stop = stops.get(symbol)
        if stop is None:
            basis = float(pos.get("avg_entry_price") or price)
            stop = round(basis * (1 - cfg.bot_stop_pct / 100), 2)
        if price <= stop:
            plan.append({"symbol": symbol, "action": "close"})
            continue
        plan.append({"symbol": symbol, "action": "stop",
                     "qty": held - covered, "stop": stop,
                     "cancel": [o["id"] for o in sells
                                if o.get("type") not in PROTECTIVE]})
    return plan


async def _wait_gone(broker, order_ids):
    """Poll until none of `order_ids` is open. True if they all cleared."""
    deadline = asyncio.get_running_loop().time() + CANCEL_SETTLE_SECONDS
    while True:
        open_ids = {o["id"] for o in await broker.open_orders() or []}
        if not open_ids & set(order_ids):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.5)


async def _apply(broker, step):
    symbol, action = step["symbol"], step["action"]
    if action == "ok":
        print(f"[protect] {symbol}: already has a broker stop")
    elif action == "short":
        print(f"[protect] !! SHORT {step['qty']} {symbol}: not touched - "
              "cover it by hand")
    elif action == "close":
        await broker.cancel_orders_for(symbol,
                                       settle_seconds=CANCEL_SETTLE_SECONDS)
        await broker.close_position(symbol)
        print(f"[protect] {symbol}: already through its stop - closed")
    elif action == "stop":
        for order_id in step["cancel"]:
            try:
                await broker.cancel_order(order_id)
            except aiohttp.ClientResponseError:
                pass
        if step["cancel"] and not await _wait_gone(broker, step["cancel"]):
            print(f"[protect] !! {symbol}: resting sells would not cancel - "
                  "NO STOP PLACED, check it by hand")
            return
        await broker.submit_stop(symbol, step["qty"], step["stop"])
        print(f"[protect] {symbol}: stop placed, {step['qty']} @ "
              f"{step['stop']:.2f}")


async def _exit_before_the_bell(broker, cfg):
    """Nobody is watching: sell what nothing is working to sell.

    The bot closes a position whose price it cannot see ("stale"); a dead
    bot sees nothing, so the same rule applies. One extended-hours limit
    each - Alpaca takes nothing else before 09:30. Whatever has not filled
    by the bell gets a real stop from protect().
    """
    orders = await broker.open_orders() or []
    working = {o.get("symbol") for o in orders if o.get("side") == "sell"}
    for pos in await broker.positions() or []:
        held = _qty(pos.get("qty"))
        if held < 1 or pos["symbol"] in working:
            continue
        price = float(pos.get("current_price") or 0)
        limit = max(0.01, round(price - cfg.bot_premarket_offset_cents, 2))
        await broker.submit_limit_sell(pos["symbol"], held, limit,
                                       extended_hours=True)
        print(f"[protect] {pos['symbol']}: bot gone before the bell - limit "
              f"sell {held} floor {limit:.2f}, extended hours")


async def protect(broker, journal, cfg, now=None):
    """Make sure no position is left without a stop by a session that ended.

    A regular-hours position's stop is an order at the broker and outlives
    the process. A pre-market one's stop WAS the process: if the session
    dies holding it, nothing guards it until the 15:50 flatten, and the
    flatten only acts inside its own window. This runs when the session
    step ends, however it ended.
    """
    now = now or dt.datetime.now(ET)
    et = now.astimezone(ET)
    if (et.hour, et.minute) >= MARKET_CLOSE:
        print("[protect] market closed - nothing can be placed")
        return
    if is_premarket(now):
        await _exit_before_the_bell(broker, cfg)
        bell = et.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                          second=15, microsecond=0)
        print(f"[protect] waiting for the bell ({(bell - et).seconds}s)")
        await asyncio.sleep((bell - et).total_seconds())
    stops = {row["symbol"]: row["stop"] for row in journal.open_trade_rows()}
    for step in plan_protection(await broker.positions() or [],
                                await broker.open_orders() or [], stops, cfg):
        try:
            await _apply(broker, step)
        except Exception as exc:
            print(f"[protect] !! {step['symbol']}: {exc!r} - check it by hand")


async def reconcile(broker, journal, position_symbols, now_ts):
    """Record exits for journal trades whose position no longer exists.

    Only fills from this trade's own lifetime count: /v2/orders answers
    newest-first across the whole account history, so an unbounded query
    averaged a previous session's exits on the same symbol into this one.
    """
    for trade in journal.open_trade_rows():
        if trade["symbol"] in position_symbols:
            continue
        legs = await broker.closed_sell_legs(trade["symbol"], trade["ts"])
        price = weighted_exit(legs)
        if price is None:
            price = trade["entry"]
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
        journal = Journal(cfg.bot_journal_path, cfg.bot_alert_window_minutes,
                          win_target_cents=(cfg.bot_scalp_target_cents
                                            if cfg.bot_scalp_mode else None))
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


async def run_protect():
    cfg = DEFAULT
    async with aiohttp.ClientSession() as session:
        broker = Broker(session, cfg)
        journal = Journal(cfg.bot_journal_path, cfg.bot_alert_window_minutes,
                          win_target_cents=(cfg.bot_scalp_target_cents
                                            if cfg.bot_scalp_mode else None))
        await protect(broker, journal, cfg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="flatten regardless of time of day")
    parser.add_argument("--protect", action="store_true",
                        help="guard positions a finished session left behind "
                             "instead of flattening")
    args = parser.parse_args()
    asyncio.run(run_protect() if args.protect else run(force=args.force))
