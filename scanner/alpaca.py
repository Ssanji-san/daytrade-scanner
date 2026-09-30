"""Alpaca market-data client (async) + response parsers.

The parsers are pure and tested; the client is a thin aiohttp wrapper
with 429 backoff. Free plan: screener endpoints are SIP-based, snapshots
and bars come from the IEX feed.
"""
import asyncio
import datetime as dt
import os

from .config import Config
from .history import ET

MAX_SYMBOLS_PER_REQUEST = 500
BARS_SYMBOLS_PER_REQUEST = 100
# Small on purpose. The news endpoint answers newest-first across every
# symbol asked for at once, and never paginates on its own - so asking for
# 150 candidates in one 50-article page buries a small cap's premarket
# catalyst under whatever the megacaps published since. Measured on a real
# session: one page covered barely two hours of a 24-hour window, with SPY
# alone taking 15 of the 100 items.
NEWS_SYMBOLS_PER_REQUEST = 25
NEWS_MAX_PAGES = 4                  # bounds the free plan's 200 req/min


# --- parsers (pure) ---

def parse_movers(raw):
    return [g["symbol"] for g in raw.get("gainers", [])]


def parse_most_actives(raw):
    return [a["symbol"] for a in raw.get("most_actives", [])]


def _quote(quote):
    """(bid, ask) from a snapshot's latestQuote, or None for either side that
    is missing or zero. A crossed quote (bid above ask) is not a price
    anyone can trade at, so both sides are dropped."""
    bid = quote.get("bp") or None
    ask = quote.get("ap") or None
    if bid and ask and bid > ask:
        return None, None
    return bid, ask


def _epoch(stamp):
    """Epoch seconds from an Alpaca timestamp, or None.

    Alpaca sends nanoseconds ("...:05.123456789Z"); fromisoformat takes at
    most microseconds, so the fraction is cut to six digits first.
    """
    if not stamp:
        return None
    text = str(stamp).replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:
        digits = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{head}.{rest[:min(digits, 6)]}{rest[digits:]}"
    try:
        return dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _bar_date(bar):
    """The ET trading date of a daily bar, or None."""
    stamp = _epoch(bar.get("t"))
    if stamp is None:
        return None
    return dt.datetime.fromtimestamp(stamp, ET).date()


def parse_snapshots(raw, today=None):
    """Snapshot rows keyed by symbol.

    `today` (an ET date) guards against yesterday's bar. Until a symbol's
    first IEX print of the day, dailyBar is yesterday's and prevDailyBar
    the day before - read naively, a stock that ran yesterday looks like it
    is gapping today, with yesterday's volume as its "relative volume". So a
    daily bar from before `today` means "not traded today yet": measured
    from yesterday's close, no volume, no high above the last price. Replay
    and backtest callers pass no date and get the bars as they are.
    """
    out = {}
    for sym, snap in raw.items():
        if not snap:
            continue
        daily = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        trade = snap.get("latestTrade") or {}
        minute = snap.get("minuteBar") or {}
        bid, ask = _quote(snap.get("latestQuote") or {})
        price = trade.get("p") or minute.get("c") or daily.get("c")
        if not price or not daily.get("h") or not prev.get("c"):
            continue
        bar_day = _bar_date(daily)
        if today is not None and bar_day is not None and bar_day < today:
            prev_close, cum_volume, day_high = daily.get("c"), 0, price
        else:
            prev_close = prev["c"]
            cum_volume, day_high = daily.get("v", 0), daily["h"]
        out[sym] = {
            "price": price,
            "cum_volume": cum_volume,
            "day_high": day_high,
            "prev_close": prev_close,
            "avg_volume": None,
            "float_shares": None,
            # Ross enters 10c above the ASK and sells at the BID pre-market,
            # where the spread is wide enough to matter. None when the feed
            # has no usable quote; callers fall back to the last trade.
            "bid": bid,
            "ask": ask,
            # When the tape last printed. The poll time says nothing about
            # this - a symbol is re-polled every cycle whether or not it
            # traded - and a stop the bot runs itself is only as live as it.
            "trade_ts": _epoch(trade.get("t")),
            # Real 1-minute OHLC: the setup detector and the honest alert
            # labels both need true highs/lows, not polled last prices.
            "minute_bar": ({"t": minute["t"], "o": minute.get("o"),
                            "h": minute.get("h"), "l": minute.get("l"),
                            "c": minute.get("c"), "v": minute.get("v", 0)}
                           if minute.get("t") and minute.get("h") else None),
        }
    return out


def parse_news(raw):
    items = []
    for article in raw.get("news", []):
        try:
            ts = int(dt.datetime.fromisoformat(
                article["created_at"].replace("Z", "+00:00")).timestamp())
        except (KeyError, ValueError):
            continue
        for sym in article.get("symbols", []):
            items.append({
                "symbol": sym,
                "headline": article.get("headline", ""),
                "ts": ts,
                "url": article.get("url", ""),
                "source": article.get("source", ""),
            })
    return items


def compute_avg_volume(bars):
    if not bars:
        return None
    return sum(b["v"] for b in bars) / len(bars)


# --- client (thin I/O) ---

class AlpacaClient:
    def __init__(self, session, cfg: Config, key=None, secret=None):
        self.session = session
        self.cfg = cfg
        self.headers = {
            "APCA-API-KEY-ID": key or os.environ.get("ALPACA_KEY", ""),
            "APCA-API-SECRET-KEY": secret or os.environ.get("ALPACA_SECRET", ""),
        }

    async def _get(self, path, params=None):
        url = self.cfg.data_base + path
        for attempt in range(4):
            async with self.session.get(url, params=params,
                                        headers=self.headers) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return await resp.json()
        raise RuntimeError(f"rate limited after retries: {path}")

    async def movers(self):
        raw = await self._get("/v1beta1/screener/stocks/movers",
                              {"top": self.cfg.movers_top})
        return parse_movers(raw)

    async def most_actives(self):
        raw = await self._get("/v1beta1/screener/stocks/most-actives",
                              {"by": "volume", "top": self.cfg.actives_top})
        return parse_most_actives(raw)

    async def snapshots(self, symbols):
        out = {}
        symbols = sorted(symbols)
        today = dt.datetime.now(ET).date()   # see parse_snapshots
        for i in range(0, len(symbols), MAX_SYMBOLS_PER_REQUEST):
            chunk = symbols[i:i + MAX_SYMBOLS_PER_REQUEST]
            raw = await self._get("/v2/stocks/snapshots",
                                  {"symbols": ",".join(chunk),
                                   "feed": self.cfg.feed})
            out.update(parse_snapshots(raw, today=today))
        return out

    async def bars(self, symbols, timeframe, start, end=None, feed=None):
        """Historical bars, chunked and paginated: {symbol: [bar, ...]}.

        `feed` overrides the configured one. The free plan serves `sip` for
        anything older than 15 minutes, which is what the backtest uses to
        measure how much of the real tape the live IEX feed misses.
        """
        out = {}
        symbols = sorted(symbols)
        # Smaller chunks than snapshots take: bar requests carry a longer
        # query string and the API rejects the whole batch with a 400 if it
        # grows too large.
        for i in range(0, len(symbols), BARS_SYMBOLS_PER_REQUEST):
            chunk = symbols[i:i + BARS_SYMBOLS_PER_REQUEST]
            token = None
            while True:
                params = {"symbols": ",".join(chunk), "timeframe": timeframe,
                          "start": start, "limit": 10000,
                          "feed": feed or self.cfg.feed,
                          "adjustment": "split"}
                if end:
                    params["end"] = end
                if token:
                    params["page_token"] = token
                raw = await self._get("/v2/stocks/bars", params)
                for sym, rows in (raw.get("bars") or {}).items():
                    out.setdefault(sym, []).extend(rows)
                token = raw.get("next_page_token")
                if not token:
                    break
        return out

    async def avg_volumes(self, symbols, days=None):
        """30-day average daily volume per symbol (rvol baseline)."""
        days = days or self.cfg.rvol_baseline_days
        start = (dt.date.today() - dt.timedelta(days=days * 2)).isoformat()
        volumes = await self.bars(symbols, "1Day", start)
        return {sym: compute_avg_volume(rows[-days:])
                for sym, rows in volumes.items()}

    async def news(self, symbols, limit=50, start=None, end=None):
        """Headlines for these symbols; defaults to the last news_max_age_hours.

        `start`/`end` let the backtest ask for a specific historical day
        instead of the trailing window the live loop wants.
        """
        if not symbols:
            return []
        if start is None:
            start = (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(hours=self.cfg.news_max_age_hours)).isoformat()
        items = []
        symbols = sorted(symbols)
        for i in range(0, len(symbols), NEWS_SYMBOLS_PER_REQUEST):
            chunk = symbols[i:i + NEWS_SYMBOLS_PER_REQUEST]
            token = None
            for _ in range(NEWS_MAX_PAGES):
                params = {"symbols": ",".join(chunk), "start": start,
                          "limit": limit}
                if end:
                    params["end"] = end
                if token:
                    params["page_token"] = token
                raw = await self._get("/v1beta1/news", params)
                items.extend(parse_news(raw))
                token = raw.get("next_page_token")
                if not token:
                    break
        return items
