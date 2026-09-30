"""Could the free feed fill a 10-second chart?

    python scripts/trade_density.py --start 2026-01-01 --end 2026-08-31

Alpaca has no 10-second bars; they would have to be built from trade
prints. A 10-second bar with no print in it is not a bar, and on IEX - a few
percent of the tape - a thin $1-5 stock may print a handful of times a
minute. This samples candidate symbol-days the backtest would replay, reads
their 09:30-10:00 prints on IEX and on SIP (free once 15 minutes old), and
reports the share of 10-second windows holding at least one trade.

The decision was fixed before running it: build the 10-second chart only if
the median candidate fills at least 80% of windows on IEX.
"""
import argparse
import asyncio
import datetime as dt
import os
import random
import statistics
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.alpaca import AlpacaClient, _epoch                # noqa: E402
from scanner.backtest import fetch                            # noqa: E402
from scanner.config import DEFAULT                            # noqa: E402
from scanner.history import ET                                # noqa: E402

GATE = 0.80
WINDOW = 10


def window_fill(trades, start, end, seconds=WINDOW):
    """(share of windows with a print, prints per window) over [start, end)."""
    total = int((end - start).total_seconds() // seconds)
    if total <= 0:
        return 0.0, 0.0
    t0, t1 = start.timestamp(), end.timestamp()
    hit, count = set(), 0
    for trade in trades:
        stamp = _epoch(trade.get("t"))
        if stamp is None or not t0 <= stamp < t1:
            continue
        hit.add(int((stamp - t0) // seconds))
        count += 1
    return len(hit) / total, count / total


async def run(start, end, samples, seed):
    cfg = DEFAULT
    cache = fetch.Cache(cfg)
    async with aiohttp.ClientSession() as session:
        client = AlpacaClient(session, cfg)
        from scanner.floats import fetch_ticker_map
        symbols = fetch.tradable_symbols(await fetch_ticker_map(session, cfg))
        lookback = (dt.date.fromisoformat(start)
                    - dt.timedelta(days=60)).isoformat()
        daily = await fetch.daily_bars(client, cache, symbols, lookback, end,
                                       "iex")
        candidates = fetch.select_candidates(daily, cfg)
        pairs = [(d, s) for d, syms in sorted(candidates.items())
                 if start <= d <= end for s in syms]
        random.Random(seed).shuffle(pairs)
        pairs = pairs[:samples]
        print(f"[density] {len(pairs)} candidate symbol-days sampled "
              f"(seed {seed}), 09:30-10:00, {WINDOW}s windows")

        fills = {"iex": [], "sip": []}
        for day, sym in pairs:
            open_ = dt.datetime.fromisoformat(f"{day}T09:30:00").replace(
                tzinfo=ET)
            close = open_ + dt.timedelta(minutes=30)
            row = []
            for feed in ("iex", "sip"):
                prints = (await client.trades(
                    [sym], open_.isoformat(), close.isoformat(), feed=feed)
                ).get(sym, [])
                fill, rate = window_fill(prints, open_, close)
                fills[feed].append(fill)
                row.append(f"{feed} {fill:5.0%} ({rate:4.1f}/win)")
            print(f"[density] {day} {sym:6} " + "  ".join(row))

    print()
    for feed, values in fills.items():
        if values:
            print(f"[density] {feed}: median fill {statistics.median(values):.0%}"
                  f", {sum(v >= GATE for v in values)}/{len(values)} "
                  f"candidates at {GATE:.0%} or more")
    median_iex = statistics.median(fills["iex"]) if fills["iex"] else 0.0
    verdict = ("PASS - IEX can carry a 10-second chart" if median_iex >= GATE
               else "FAIL - a 10-second chart needs the real-time SIP feed")
    print(f"[density] gate: median IEX fill >= {GATE:.0%} -> {verdict}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    asyncio.run(run(args.start, args.end, args.samples, args.seed))


if __name__ == "__main__":
    main()
