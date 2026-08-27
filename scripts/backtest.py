"""Replay historical sessions to build training data.

    python scripts/backtest.py --start 2026-08-01 --end 2026-08-31

Writes graded alerts to `cache/backtest.db` - never the live journal, so a
biased replay cannot quietly poison what the bot learned from real sessions.

    --feed iex   (default) matches live exactly; train on this
    --feed sip   the real consolidated tape, for measuring what IEX misses
    --fetch-only download and cache without replaying
"""
import argparse
import asyncio
import os
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.alpaca import AlpacaClient                       # noqa: E402
from scanner.backtest import fetch, replay                    # noqa: E402
from scanner.config import DEFAULT                            # noqa: E402
from scanner.floats import FloatCache                         # noqa: E402
from scanner.trading.journal import Journal                   # noqa: E402
from scanner.trading.model import train                       # noqa: E402


def _context_for(day, daily, floats, cfg):
    """What a live session would already know at the open on `day`.

    Everything here must come from data strictly before `day`; the previous
    close and the volume baseline are exactly the values a peeking backtest
    gets wrong.
    """
    prev_close, avg_volume, float_shares = {}, {}, {}
    for symbol, rows in daily.items():
        earlier = sorted((r for r in rows if r.get("t") and r["t"][:10] < day),
                         key=lambda r: r["t"])
        if not earlier:
            continue
        prev_close[symbol] = earlier[-1].get("c")
        avg_volume[symbol] = fetch.prior_avg_volume(
            rows, day, cfg.rvol_baseline_days)
        float_shares[symbol] = floats.get(symbol)
    return {"prev_close": prev_close, "avg_volume": avg_volume,
            "float_shares": float_shares}


async def run(start, end, feed, fetch_only):
    cfg = DEFAULT
    cache = fetch.Cache(cfg)
    journal = Journal(cfg.backtest_journal_path, cfg.bot_alert_window_minutes)
    floats = FloatCache(cfg)

    async with aiohttp.ClientSession() as session:
        client = AlpacaClient(session, cfg)

        print(f"[backtest] universe from daily bars ({feed}) {start} -> {end}")
        from scanner.floats import fetch_ticker_map
        tickers = await fetch_ticker_map(session, cfg)
        symbols = fetch.tradable_symbols(tickers)
        print(f"[backtest] {len(symbols)} common-stock symbols "
              f"(of {len(tickers)} in the SEC map)")

        daily = await fetch.daily_bars(client, cache, symbols, start, end, feed)
        print(f"[backtest] daily bars for {len(daily)} symbols")

        candidates = fetch.select_candidates(daily, cfg)
        days = sorted(candidates)
        total = sum(len(v) for v in candidates.values())
        print(f"[backtest] {total} symbol-days across {len(days)} sessions")
        if not days:
            print("[backtest] nothing qualified - widen the window")
            return

        for day in days:
            todays = candidates[day]
            minute = await fetch.minute_bars(client, cache, todays, day, feed)
            news = await fetch.day_news(client, cache, todays, day)
            if fetch_only:
                print(f"[backtest] {day}: cached {len(minute)} symbols, "
                      f"{len(news)} headlines")
                continue
            context = _context_for(day, daily, floats, cfg)
            graded = replay.replay_day(day, minute, news, context, journal, cfg)
            print(f"[backtest] {day}: {len(todays):>3} candidates -> "
                  f"{graded:>4} alerts journalled")

    if fetch_only:
        return
    report(journal, cfg)


def _expectancy(rows):
    """(hit rate, pure-2R R, runner R) over graded alerts.

    Pure 2R: +2 on a win, -1 otherwise. The runner banks half at +2R and
    rides the rest, so it is credited with mfe less a rough 1R of trail
    give-back. mfe is a floor here, not a ceiling - it stops at the last bar
    of the tracking window.
    """
    if not rows:
        return 0.0, 0.0, 0.0
    wins = sum(1 for r in rows if r["label"] == 1)
    pure = sum(2 if r["label"] == 1 else -1 for r in rows) / len(rows)
    runner = 0.0
    for r in rows:
        if r["label"] != 1:
            runner -= 1
            continue
        ran = (r["mfe"] / r["r_dollars"]) if r["r_dollars"] else 2.0
        runner += 1.0 + 0.5 * max(2.0, ran - 1.0)
    return wins / len(rows), pure, runner / len(rows)


def _loss_split(rows):
    """(stopped out, timed out) among the losses.

    A loss whose worst excursion never reached -1R did not lose - the clock
    ran out on it. That was 45% of losses at the old 30-minute horizon, and
    it is the number this longer hold is meant to move.
    """
    stopped = timed = 0
    for r in rows:
        if r["label"] != 0:
            continue
        if r["r_dollars"] and r["mae"] <= -r["r_dollars"]:
            stopped += 1
        else:
            timed += 1
    return stopped, timed


def report(journal, cfg):
    """What the replay found, in R and in dollars."""
    rows = journal.outcome_rows()
    dataset = journal.labeled_dataset()
    print()
    if not rows:
        print("[backtest] nothing labeled yet")
        return
    risk = cfg.bot_bankroll * cfg.bot_risk_pct / 100
    print(f"[backtest] {len(rows)} labeled alerts, ${risk:,.0f} risked per "
          f"trade, {cfg.bot_alert_window_minutes}-minute horizon")
    print(f"[backtest] {'month':>8} {'n':>5} {'hit':>7} {'2R exp':>9} "
          f"{'runner':>9} {'$/trade':>9}  {'stopped':>8} {'timeout':>8}")
    by_month = {}
    for row in rows:
        by_month.setdefault(row["day"][:7], []).append(row)
    for month in sorted(by_month) + ["ALL"]:
        block = rows if month == "ALL" else by_month[month]
        hit, pure, runner = _expectancy(block)
        stopped, timed = _loss_split(block)
        print(f"[backtest] {month:>8} {len(block):>5} {hit:>6.1%} "
              f"{pure:>+8.2f}R {runner:>+8.2f}R {runner * risk:>+8.0f} "
              f"{stopped:>8} {timed:>8}")
    print("[backtest] break-even on a 2R target needs a 33.3% hit rate")

    if len(dataset) >= cfg.bot_model_min_samples:
        _, meta = train(dataset, min_samples=cfg.bot_model_min_samples,
                        percentile=cfg.bot_score_percentile)
        print(f"[backtest] model: {meta['kind']} samples={meta['samples']} "
              f"holdout={meta.get('holdout_acc')} bar={meta.get('threshold')}")
    for row in journal.setup_stats():
        print(f"[backtest]   {row['setup']:16} n={row['n']:<4} "
              f"wins={row['wins']} exp_r={row['exp_r']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--feed", default="iex", choices=("iex", "sip"),
                        help="iex matches live; sip measures what it misses")
    parser.add_argument("--fetch-only", action="store_true",
                        help="download and cache without replaying")
    args = parser.parse_args()
    asyncio.run(run(args.start, args.end, args.feed, args.fetch_only))


if __name__ == "__main__":
    main()
