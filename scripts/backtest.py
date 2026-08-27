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
import datetime as dt
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


# rvol is measured against a 30-SESSION baseline, and prev_close needs the
# day before. Fetching daily bars from the replay's own start date would
# leave the first weeks of every run with a baseline of one or two days -
# which is exactly what a month-at-a-time schedule would produce. 60
# calendar days covers 30 sessions plus holidays.
BASELINE_LOOKBACK_DAYS = 60


def _lookback_start(start, days=BASELINE_LOOKBACK_DAYS):
    return (dt.date.fromisoformat(start) - dt.timedelta(days=days)).isoformat()


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

        history_start = _lookback_start(start)
        daily = await fetch.daily_bars(client, cache, symbols, history_start,
                                       end, feed)
        print(f"[backtest] daily bars for {len(daily)} symbols "
              f"(from {history_start} for the volume baseline)")

        candidates = fetch.select_candidates(daily, cfg)
        # The lookback feeds the baseline only; never replay those sessions.
        days = [d for d in sorted(candidates) if start <= d <= end]
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


def outcome(row):
    """"win", "stopped" or "timeout" for one graded alert.

    The journal's label is binary - anything that is not a win is a 0 - but
    those two zeros are not the same trade. A stop-out really lost 1R. A
    timeout is a position the time stop closes at whatever the market is
    then, which is usually near break-even. Scoring them alike is the
    difference between a strategy that bleeds and one that mostly does
    nothing.
    """
    if row["label"] == 1:
        return "win"
    if row["r_dollars"] and row["mae"] <= -row["r_dollars"]:
        return "stopped"
    return "timeout"


def _expectancy(rows, timeout_r):
    """(hit rate, pure-2R R, runner R), scoring timeouts at `timeout_r`.

    The runner banks half at +2R and rides the rest, credited with mfe less
    a rough 1R of trail give-back. mfe is a floor, not a ceiling: it stops
    at the last bar of the tracking window.
    """
    if not rows:
        return 0.0, 0.0, 0.0
    wins = pure = runner = 0
    for row in rows:
        kind = outcome(row)
        if kind == "win":
            wins += 1
            pure += 2
            ran = (row["mfe"] / row["r_dollars"]) if row["r_dollars"] else 2.0
            runner += 1.0 + 0.5 * max(2.0, ran - 1.0)
        elif kind == "stopped":
            pure -= 1
            runner -= 1
        else:
            pure += timeout_r
            runner += timeout_r
    n = len(rows)
    return wins / n, pure / n, runner / n


def _counts(rows):
    tally = {"win": 0, "stopped": 0, "timeout": 0}
    for row in rows:
        tally[outcome(row)] += 1
    return tally


def report(journal, cfg):
    """What the replay found, in R and in dollars."""
    rows = journal.outcome_rows()
    dataset = journal.labeled_dataset()
    print()
    if not rows:
        print("[backtest] nothing labeled yet")
        return
    risk = cfg.bot_bankroll * cfg.bot_risk_pct / 100
    print(f"[backtest] {len(rows)} graded alerts, ${risk:,.0f} risked per "
          f"trade, {cfg.bot_alert_window_minutes}-minute horizon, "
          f"{cfg.bot_stop_pct:.0f}% stop")
    print("[backtest] 'label' scores a timeout as a full -1R the way the "
          "journal does;")
    print("[backtest] 'timestop' scores it at 0R, which is closer to what "
          "the exit really does.")
    print(f"[backtest] {'month':>8} {'n':>6} {'win':>6} {'stop':>6} "
          f"{'t/out':>6} {'label':>8} {'timestop':>9} {'runner':>8} "
          f"{'$/trade':>8}")
    by_month = {}
    for row in rows:
        by_month.setdefault(row["day"][:7], []).append(row)
    for month in sorted(by_month) + ["ALL"]:
        block = rows if month == "ALL" else by_month[month]
        tally = _counts(block)
        hit, label_r, _ = _expectancy(block, timeout_r=-1.0)
        _, stop_r, runner_r = _expectancy(block, timeout_r=0.0)
        n = len(block)
        print(f"[backtest] {month:>8} {n:>6} {hit:>5.1%} "
              f"{tally['stopped'] / n:>5.1%} {tally['timeout'] / n:>5.1%} "
              f"{label_r:>+7.2f}R {stop_r:>+8.2f}R {runner_r:>+7.2f}R "
              f"{runner_r * risk:>+7.0f}")
    print("[backtest] break-even on a 2R target needs a 33.3% win rate")

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
