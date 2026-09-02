"""Fill the float cache for the whole tradable universe.

    python scripts/fetch_floats.py [--rate 5] [--limit N]

Unknown float is an automatic rejection in `hod.scan`, so a symbol nobody has
looked up yet is indistinguishable from a symbol with a billion shares. The
live loop only fetches `FLOAT_FETCHES_PER_CYCLE = 4` symbols per three-second
cycle and has never worked through the list: the cache held 1,981 symbols
while 44% of replayed alerts had no float at all, and names like IQ, JZXN and
MEGL were simply absent.

This walks the universe once and fills it. Entries last `float_cache_days`,
so a weekly run only has to top up whatever went stale.

SEC asks automated clients to identify themselves (SEC_CONTACT) and to stay
under 10 requests a second. One symbol costs one to three requests, so the
default of 5 symbols a second sits comfortably under that ceiling even in the
worst case.
"""
import argparse
import asyncio
import os
import sys
import time

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.backtest.fetch import tradable_symbols                # noqa: E402
from scanner.config import DEFAULT                                 # noqa: E402
from scanner.floats import (FloatCache, fetch_shares,              # noqa: E402
                            fetch_ticker_map)

SAVE_EVERY = 250


def coverage(cache, symbols):
    """(have a figure, asked but no figure, never asked)."""
    have = asked = missing = 0
    for symbol in symbols:
        entry = cache._data.get(symbol)
        if entry is None:
            missing += 1
        elif entry.get("shares"):
            have += 1
        else:
            asked += 1
    return have, asked, missing


async def run(rate, limit):
    cfg = DEFAULT
    cache = FloatCache(cfg)

    async with aiohttp.ClientSession() as session:
        tickers = await fetch_ticker_map(session, cfg)
        symbols = tradable_symbols(tickers)
        have, asked, missing = coverage(cache, symbols)
        print(f"[floats] universe {len(symbols)} common-stock symbols")
        print(f"[floats] before: {have} with a figure, {asked} asked without "
              f"one, {missing} never asked")

        todo = [s for s in symbols if cache.is_stale(s)]
        if limit:
            todo = todo[:limit]
        print(f"[floats] {len(todo)} stale or unknown - fetching at "
              f"{rate}/s, about {len(todo) / rate / 60:.0f} min")

        interval = 1.0 / rate
        found = failed = 0
        for i, symbol in enumerate(todo, 1):
            started = time.monotonic()
            try:
                shares, answered = await fetch_shares(session, tickers[symbol])
            except Exception as exc:                 # never abort the run
                print(f"[floats] {symbol}: {type(exc).__name__}: {exc}")
                shares, answered = None, False
            cache.put(symbol, shares, answered=answered, flush=False)
            found += bool(shares)
            failed += not answered
            if i % SAVE_EVERY == 0:
                cache.save()                          # survive a cancellation
                print(f"[floats] {i}/{len(todo)} ... {found} found")
            elapsed = time.monotonic() - started
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
        cache.save()

    have, asked, missing = coverage(cache, symbols)
    print(f"[floats] fetched {len(todo)}: {found} with a figure, "
          f"{failed} unanswered (retried within the hour)")
    print(f"[floats] after:  {have} with a figure, {asked} asked without "
          f"one, {missing} never asked")
    print(f"[floats] coverage {100 * have / len(symbols):.1f}% of the universe")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=5.0,
                        help="symbols per second; SEC's ceiling is 10 requests/s"
                             " and a symbol costs up to 3")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N symbols (for a smoke test)")
    args = parser.parse_args()
    asyncio.run(run(args.rate, args.limit))


if __name__ == "__main__":
    main()
