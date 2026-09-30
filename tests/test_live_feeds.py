"""The live loop's two new feeds: breaking news and the real tape.

Both are thin I/O over pure pieces tested elsewhere; these check the
bookkeeping - what gets asked for, from when, and what reaches the state.
"""
import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

from scanner.config import Config
from scanner.main import (discover_news, news_candidates, refresh_sip,
                          watchlist)
from scanner.state import MarketState

ET = ZoneInfo("America/New_York")
CFG = Config()


def et(h, m):
    return dt.datetime(2026, 7, 14, h, m, tzinfo=ET)


class FakeClient:
    def __init__(self, news=(), bars=None):
        self._news, self._bars = list(news), bars or {}
        self.news_calls, self.bar_calls = [], []

    async def market_news(self, since):
        self.news_calls.append(since)
        return list(self._news)

    async def bars(self, symbols, timeframe, start, end=None, feed=None):
        self.bar_calls.append((sorted(symbols), timeframe, start, end, feed))
        return {s: self._bars.get(s, []) for s in symbols if s in self._bars}


def item(symbol, when, headline="SOS signs deal"):
    return {"symbol": symbol, "headline": headline, "url": "u",
            "source": "benzinga", "ts": int(when.timestamp())}


class TestBreakingNews:
    def test_a_headline_makes_its_stock_a_candidate(self):
        state, seen = MarketState(CFG), {}
        client = FakeClient([item("SOS", et(7, 58)), item("ETF", et(7, 59))])
        since = asyncio.run(discover_news(
            client, state, seen, {"SOS": 1, "AAPL": 2}, 100, et(8, 0)))
        assert seen == {"SOS": int(et(7, 58).timestamp())}   # ETF not common
        assert since == int(et(7, 59).timestamp())
        assert client.news_calls == [
            dt.datetime.fromtimestamp(100, dt.timezone.utc).isoformat()]
        assert "SOS" in state._news_by_symbol           # scored as a catalyst

    def test_nothing_new_keeps_the_same_since(self):
        since = asyncio.run(discover_news(
            FakeClient(), MarketState(CFG), {}, {}, 500, et(8, 0)))
        assert since == 500

    def test_candidates_last_while_the_news_is_young(self):
        seen = {"OLD": int(et(3, 0).timestamp()),
                "NEW": int(et(7, 0).timestamp())}
        assert news_candidates(seen, et(8, 0), CFG) == ["NEW"]

    def test_a_stock_far_outside_the_price_band_is_dropped(self):
        """News finds every megacap; the scan can only use $1-$10 stocks.
        Watching AAPL costs snapshot and news calls for nothing."""
        when = int(et(7, 0).timestamp())
        seen = {"AAPL": when, "SOS": when, "PENY": when, "FRESH": when}
        prices = {"AAPL": 230.0, "SOS": 2.10, "PENY": 0.20}
        assert news_candidates(seen, et(8, 0), CFG, prices) == ["FRESH", "SOS"]

    def test_near_the_band_is_kept(self):
        """A $0.90 stock with news can run through $1; keep watching it."""
        when = int(et(7, 0).timestamp())
        assert news_candidates({"ALMO": when}, et(8, 0), CFG,
                               {"ALMO": 0.90}) == ["ALMO"]


class TestRealTape:
    SIP = {"VOL": [{"t": "2026-07-14T08:30:00Z", "v": 700_000}]}

    def test_first_read_starts_at_4am_and_stops_16_minutes_back(self):
        state, until = MarketState(CFG), {}
        client = FakeClient(bars=self.SIP)
        asyncio.run(refresh_sip(client, state, ["VOL", "QUIET"], until,
                                et(8, 0)))
        (syms, tf, start, end, feed), = client.bar_calls
        assert (syms, tf, feed) == (["QUIET", "VOL"], "1Min", "sip")
        assert start == et(4, 0).isoformat()
        assert end == et(7, 44).isoformat()
        assert until == {"VOL": et(7, 44), "QUIET": et(7, 44)}
        assert state._sip["VOL"].before(et(7, 44)) == 700_000
        assert state._sip["QUIET"].until == et(7, 44)   # read, and empty

    def test_later_reads_pick_up_where_the_last_stopped(self):
        state, until = MarketState(CFG), {}
        client = FakeClient(bars=self.SIP)
        asyncio.run(refresh_sip(client, state, ["VOL"], until, et(8, 0)))
        asyncio.run(refresh_sip(client, state, ["VOL", "NEWB"], until,
                                et(8, 5)))
        starts = {tuple(c[0]): c[2] for c in client.bar_calls[1:]}
        assert starts == {("VOL",): et(7, 44).isoformat(),
                          ("NEWB",): et(4, 0).isoformat()}


class TestWatchlist:
    """A held stock is watched for as long as it is held: the candle exit
    reads its bars, and bars only come for symbols still snapshotted."""

    def test_an_expired_symbol_drops_off(self):
        stale = et(9, 0)
        assert watchlist({"OLD": stale}, et(10, 0), CFG) == {}

    def test_a_held_symbol_stays_whatever_its_age(self):
        out = watchlist({"HELD": et(9, 0)}, et(10, 0), CFG, held={"HELD"})
        assert out == {"HELD": et(10, 0)}

    def test_a_fresh_symbol_stays(self):
        seen = et(9, 55)
        assert watchlist({"NEW": seen}, et(10, 0), CFG) == {"NEW": seen}
