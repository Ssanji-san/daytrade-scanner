"""Historical fetching with a disk cache.

There is no historical screener - `movers()` and `most_actives()` only answer
for right now - so the candidate universe has to be rebuilt from daily bars:
for each session, which symbols were up enough, in the price band, to be worth
looking at intraday.

Everything is cached under `Config.backtest_cache_dir`. Refetching thousands
of symbol-days at 200 requests a minute is slow, and a replay gets re-run
often while the logic is being tuned.
"""
import datetime as dt
import json
import pathlib
import re

from ..config import Config

# Common stock only: 1-5 plain letters. The SEC map also lists preferreds,
# warrants and units (ABR-PD, ACHR-WT, AAC-UN), which the bars endpoint
# rejects and which this strategy would not trade anyway - WVVIP was the
# lesson that a preferred can print a huge percentage move on no volume.
COMMON_STOCK = re.compile(r"^[A-Z]{1,5}$")


def tradable_symbols(tickers):
    """Drop the share classes this strategy has no business trading."""
    return sorted(s for s in tickers if COMMON_STOCK.match(s))


# Pure helpers first - these are what the tests exercise.


def day_change_pct(bar, prev_close):
    """Close-to-close move for one daily bar, or None without a baseline."""
    if not prev_close or not bar or not bar.get("c"):
        return None
    return 100.0 * (bar["c"] - prev_close) / prev_close


def day_high_pct(bar, prev_close):
    """How far the session ran at its best, measured from the prior close.

    This is what a live gainers screener reacts to. Close-to-close misses a
    stock that ran 30% and gave it all back, and those are exactly the
    momentum days the scanner exists to catch.
    """
    if not prev_close or not bar or not bar.get("h"):
        return None
    return 100.0 * (bar["h"] - prev_close) / prev_close


def select_candidates(daily_bars, cfg: Config):
    """{date: [symbol]} - who was worth watching on each session.

    Stands in for the screener the live loop gets for free. Deliberately
    looser than the HOD gate: this only narrows the universe enough to make
    intraday fetching affordable, and the real criteria are applied during
    the replay itself.

    Selection uses the session HIGH, not its close, because that is what a
    live gainers list reacts to. This is a universe-construction step, not a
    signal: it decides which symbols are worth pulling minute bars for, and
    every actual criterion is still evaluated point-in-time during the
    replay. It carries the same selection bias the live scanner has - only
    movers are ever looked at - which is the bias we want to reproduce.
    """
    by_day = {}
    for symbol, rows in daily_bars.items():
        rows = [r for r in rows if r.get("t") and r.get("c")]
        rows.sort(key=lambda r: r["t"])
        for i in range(1, len(rows)):
            bar, prev = rows[i], rows[i - 1]
            run_up = day_high_pct(bar, prev.get("c"))
            if run_up is None or run_up < cfg.hod_min_pct_up:
                continue
            low = bar.get("l") or bar["c"]
            if bar["h"] < cfg.hod_min_price or low > cfg.hod_max_price:
                continue          # never inside the tradable band all day
            by_day.setdefault(bar["t"][:10], []).append(symbol)
    return {day: sorted(set(symbols)) for day, symbols in by_day.items()}


def prior_avg_volume(rows, day, days):
    """Average daily volume BEFORE `day` - never including the day itself.

    Using the simulated session's own volume would leak the outcome into the
    feature that helps predict it.
    """
    earlier = [r["v"] for r in rows
               if r.get("t") and r["t"][:10] < day and r.get("v")]
    if not earlier:
        return None
    window = earlier[-days:]
    return sum(window) / len(window)


class Cache:
    """Plain JSON on disk, keyed by whatever the caller names the slice."""

    def __init__(self, cfg: Config):
        self.root = pathlib.Path(cfg.backtest_cache_dir)

    def path(self, *parts):
        return self.root.joinpath(*parts).with_suffix(".json")

    def get(self, *parts):
        path = self.path(*parts)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None          # half-written file, refetch it

    def put(self, value, *parts):
        path = self.path(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, separators=(",", ":")),
                        encoding="utf-8")
        return value


async def daily_bars(client, cache: Cache, symbols, start, end, feed):
    """Daily bars for the whole period, cached as one slice per feed."""
    key = ("daily", f"{feed}-{start}-{end}")
    cached = cache.get(*key)
    if cached is not None:
        return cached
    rows = await client.bars(symbols, "1Day", start, end=end, feed=feed)
    return cache.put(rows, *key)


async def minute_bars(client, cache: Cache, symbols, day, feed):
    """One session of 1-minute bars, cached per day and feed."""
    key = ("minute", feed, day)
    cached = cache.get(*key)
    if cached is not None:
        return cached
    start = f"{day}T08:00:00Z"          # 04:00 ET, covers premarket
    end = f"{day}T21:00:00Z"            # 17:00 ET, covers the close
    rows = await client.bars(symbols, "1Min", start, end=end, feed=feed)
    return cache.put(rows, *key)


async def day_news(client, cache: Cache, symbols, day):
    """Headlines published on `day`, cached per day."""
    key = ("news", day)
    cached = cache.get(*key)
    if cached is not None:
        return cached
    items = await client.news(symbols, limit=50,
                              start=f"{day}T00:00:00Z",
                              end=f"{day}T23:59:59Z")
    return cache.put(items, *key)


def trading_days(daily_bars_by_symbol):
    """Sessions that actually have data, oldest first."""
    days = set()
    for rows in daily_bars_by_symbol.values():
        for row in rows:
            if row.get("t"):
                days.add(row["t"][:10])
    return sorted(days)


def parse_day(text):
    return dt.datetime.strptime(text, "%Y-%m-%d").date()
