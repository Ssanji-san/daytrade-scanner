"""Alpaca market-data client (async) + response parsers.

The parsers are pure and tested; the client is a thin aiohttp wrapper
with 429 backoff. Free plan: screener endpoints are SIP-based, snapshots
and bars come from the IEX feed.
"""
import asyncio
import datetime as dt
import os

from .config import Config

MAX_SYMBOLS_PER_REQUEST = 500


# --- parsers (pure) ---

def parse_movers(raw):
    return [g["symbol"] for g in raw.get("gainers", [])]


def parse_most_actives(raw):
    return [a["symbol"] for a in raw.get("most_actives", [])]


def parse_snapshots(raw):
    out = {}
    for sym, snap in raw.items():
        if not snap:
            continue
        daily = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        trade = snap.get("latestTrade") or {}
        minute = snap.get("minuteBar") or {}
        price = trade.get("p") or minute.get("c") or daily.get("c")
        if not price or not daily.get("h") or not prev.get("c"):
            continue
        out[sym] = {
            "price": price,
            "cum_volume": daily.get("v", 0),
            "day_high": daily["h"],
            "prev_close": prev["c"],
            "avg_volume": None,
            "float_shares": None,
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
        for i in range(0, len(symbols), MAX_SYMBOLS_PER_REQUEST):
            chunk = symbols[i:i + MAX_SYMBOLS_PER_REQUEST]
            raw = await self._get("/v2/stocks/snapshots",
                                  {"symbols": ",".join(chunk),
                                   "feed": self.cfg.feed})
            out.update(parse_snapshots(raw))
        return out

    async def avg_volumes(self, symbols, days=None):
        """30-day average daily volume per symbol (rvol baseline)."""
        days = days or self.cfg.rvol_baseline_days
        start = (dt.date.today() - dt.timedelta(days=days * 2)).isoformat()
        volumes, token = {}, None
        while True:
            params = {"symbols": ",".join(sorted(symbols)), "timeframe": "1Day",
                      "start": start, "limit": 10000, "feed": self.cfg.feed,
                      "adjustment": "split"}
            if token:
                params["page_token"] = token
            raw = await self._get("/v2/stocks/bars", params)
            for sym, bars in (raw.get("bars") or {}).items():
                volumes.setdefault(sym, []).extend(bars)
            token = raw.get("next_page_token")
            if not token:
                break
        return {sym: compute_avg_volume(bars[-days:])
                for sym, bars in volumes.items()}

    async def news(self, symbols, limit=50):
        if not symbols:
            return []
        start = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(hours=self.cfg.news_max_age_hours)).isoformat()
        raw = await self._get("/v1beta1/news",
                              {"symbols": ",".join(sorted(symbols)),
                               "start": start, "limit": limit})
        return parse_news(raw)
