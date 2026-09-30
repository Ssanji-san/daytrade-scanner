import asyncio
import datetime as dt

from scanner.alpaca import (NEWS_MAX_PAGES, NEWS_SYMBOLS_PER_REQUEST,
                            AlpacaClient, compute_avg_volume, parse_movers,
                            parse_most_actives, parse_news, parse_snapshots)
from scanner.config import Config

CFG = Config()


def test_parse_movers_returns_gainer_symbols():
    raw = {"gainers": [{"symbol": "AAA", "percent_change": 25.0},
                       {"symbol": "BBB", "percent_change": 12.0}],
           "losers": [{"symbol": "ZZZ", "percent_change": -30.0}]}
    assert parse_movers(raw) == ["AAA", "BBB"]


def test_parse_most_actives():
    raw = {"most_actives": [{"symbol": "AAA", "volume": 1}, {"symbol": "CCC", "volume": 2}]}
    assert parse_most_actives(raw) == ["AAA", "CCC"]


def test_parse_snapshots_maps_fields():
    raw = {"AAA": {
        "latestTrade": {"p": 5.43, "t": "2026-07-14T15:59:00Z"},
        "dailyBar": {"o": 4.2, "h": 5.60, "l": 4.1, "c": 5.43, "v": 1_234_567},
        "prevDailyBar": {"c": 4.00, "v": 800_000},
        "minuteBar": {"c": 5.42, "v": 1000},
    }}
    out = parse_snapshots(raw)
    assert out["AAA"] == {"price": 5.43, "cum_volume": 1_234_567,
                          "day_high": 5.60, "prev_close": 4.00,
                          "avg_volume": None, "float_shares": None,
                          "bid": None, "ask": None,   # no latestQuote here
                          # 2026-07-14T15:59:00Z, when the tape printed
                          "trade_ts": 1784044740.0,
                          "prev_high": None,    # prevDailyBar has no h
                          "minute_bar": None}   # no t/h on this bar


def test_parse_snapshots_falls_back_when_no_latest_trade():
    raw = {"AAA": {"dailyBar": {"h": 5.6, "c": 5.5, "v": 100},
                   "prevDailyBar": {"c": 4.0}}}
    assert parse_snapshots(raw)["AAA"]["price"] == 5.5


def test_parse_snapshots_skips_unusable_entries():
    raw = {"AAA": {"prevDailyBar": {"c": 4.0}}, "BBB": None,
           "CCC": {"dailyBar": {"h": 1.0, "c": 1.0, "v": 5}}}  # no prev close
    assert parse_snapshots(raw) == {}


def test_parse_news_expands_symbols():
    raw = {"news": [{"headline": "Big deal", "symbols": ["AAA", "BBB"],
                     "created_at": "2026-07-14T12:00:00Z", "url": "u",
                     "source": "benzinga"}]}
    items = parse_news(raw)
    assert [i["symbol"] for i in items] == ["AAA", "BBB"]
    assert items[0]["headline"] == "Big deal"
    assert items[0]["ts"] == 1784030400


def test_compute_avg_volume():
    bars = [{"v": 100}, {"v": 200}, {"v": 300}]
    assert compute_avg_volume(bars) == 200
    assert compute_avg_volume([]) is None


class TestNewsIsNotCrowdedOut:
    """One page of 50 articles for every candidate at once is not 24 hours.

    Measured on a real session: the window asked for was 24 hours and what
    came back spanned barely two, with SPY alone taking 15 of the 100 items.
    A small cap's premarket catalyst - the thing this strategy trades - was
    invisible by mid-morning, and the bot always scans with news required.
    """

    def _client(self, pages):
        """An AlpacaClient over a session that replays `pages` in order."""
        calls = []

        class FakeResponse:
            def __init__(self, body):
                self.status = 200
                self._body = body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            async def json(self):
                return self._body

        class FakeSession:
            def get(self, url, params=None, headers=None):
                calls.append(dict(params or {}))
                return FakeResponse(pages[len(calls) - 1])

        return AlpacaClient(FakeSession(), CFG), calls

    def _article(self, symbol, headline="h"):
        return {"headline": headline, "symbols": [symbol],
                "created_at": "2026-07-14T12:00:00Z", "url": "u",
                "source": "benzinga"}

    def test_symbols_are_asked_for_in_chunks(self):
        symbols = [f"S{i:03d}" for i in range(NEWS_SYMBOLS_PER_REQUEST * 2 + 1)]
        pages = [{"news": [self._article(s)]} for s in ("A", "B", "C")]
        client, calls = self._client(pages)

        items = asyncio.run(client.news(symbols, start="2026-07-14T00:00:00Z"))

        assert len(calls) == 3
        assert all(len(c["symbols"].split(",")) <= NEWS_SYMBOLS_PER_REQUEST
                   for c in calls)
        assert [i["symbol"] for i in items] == ["A", "B", "C"]

    def test_a_chunk_is_paginated_to_the_end(self):
        pages = [{"news": [self._article("AAA")], "next_page_token": "t1"},
                 {"news": [self._article("BBB")]}]
        client, calls = self._client(pages)

        items = asyncio.run(client.news(["AAA"], start="2026-07-14T00:00:00Z"))

        assert [i["symbol"] for i in items] == ["AAA", "BBB"]
        assert calls[1]["page_token"] == "t1"

    def test_pagination_is_bounded(self):
        endless = [{"news": [self._article("AAA")], "next_page_token": "t"}] * 50
        client, calls = self._client(endless)
        asyncio.run(client.news(["AAA"], start="2026-07-14T00:00:00Z"))
        assert len(calls) == NEWS_MAX_PAGES


    def test_market_news_asks_for_every_symbol(self):
        """Breaking-news discovery: no symbol list, so the whole market."""
        pages = [{"news": [self._article("AAA")], "next_page_token": "t1"},
                 {"news": [self._article("BBB")]}]
        client, calls = self._client(pages)
        items = asyncio.run(client.market_news("2026-07-14T11:00:00+00:00"))
        assert [i["symbol"] for i in items] == ["AAA", "BBB"]
        assert "symbols" not in calls[0]
        assert calls[0]["start"] == "2026-07-14T11:00:00+00:00"
        assert calls[1]["page_token"] == "t1"

    def test_market_news_is_bounded(self):
        endless = [{"news": [self._article("AAA")], "next_page_token": "t"}] * 50
        client, calls = self._client(endless)
        asyncio.run(client.market_news("2026-07-14T11:00:00+00:00"))
        assert len(calls) == NEWS_MAX_PAGES


class TestQuotes:
    """Pre-market Ross buys 10c over the ASK and sells under the BID, where
    the spread is wide enough to matter - so the quote has to come through."""

    def _snap(self, quote):
        return {"AAA": {"latestTrade": {"p": 2.00},
                        "latestQuote": quote,
                        "dailyBar": {"h": 2.10, "c": 2.00, "v": 100},
                        "prevDailyBar": {"c": 1.50}}}

    def test_bid_and_ask_come_through(self):
        out = parse_snapshots(self._snap({"bp": 1.98, "ap": 2.03}))["AAA"]
        assert (out["bid"], out["ask"]) == (1.98, 2.03)

    def test_a_missing_or_zero_side_is_none(self):
        out = parse_snapshots(self._snap({"bp": 0, "ap": 2.03}))["AAA"]
        assert (out["bid"], out["ask"]) == (None, 2.03)

    def test_a_crossed_quote_is_dropped(self):
        """Bid above ask is not a price anyone can trade at."""
        out = parse_snapshots(self._snap({"bp": 2.05, "ap": 2.01}))["AAA"]
        assert (out["bid"], out["ask"]) == (None, None)


class TestYesterdaysBarIsNotToday:
    """Before a symbol's first IEX print of the day, the snapshot's dailyBar
    is YESTERDAY and prevDailyBar the day before. Measured against that, a
    stock that ran yesterday read as gapping today: live on 2026-09-29 ABLV
    showed +30% and KNRX +230% at 07:30, when they were flat and -12%."""

    SNAP = {"KNRX": {
        "latestTrade": {"p": 1.03, "t": "2026-09-29T11:25:00Z"},
        # yesterday (09-28): 0.32 -> 1.175 on 40M shares
        "dailyBar": {"t": "2026-09-28T04:00:00Z", "o": 0.32, "h": 1.40,
                     "l": 0.31, "c": 1.175, "v": 40_000_000},
        "prevDailyBar": {"t": "2026-09-25T04:00:00Z", "c": 0.312},
    }}

    def test_measured_against_yesterdays_close(self):
        out = parse_snapshots(self.SNAP, today=dt.date(2026, 9, 29))["KNRX"]
        assert out["prev_close"] == 1.175           # down 12%, not up 230%

    def test_no_volume_and_no_high_carried_over(self):
        out = parse_snapshots(self.SNAP, today=dt.date(2026, 9, 29))["KNRX"]
        assert out["cum_volume"] == 0               # yesterday's 40M is not rvol
        assert out["day_high"] == 1.03              # nor is yesterday's high

    def test_yesterdays_high_is_the_daily_bar_before_today(self):
        out = parse_snapshots(self.SNAP, today=dt.date(2026, 9, 29))["KNRX"]
        assert out["prev_high"] == 1.40

    def test_yesterdays_high_is_the_prev_bar_once_today_trades(self):
        snap = {"KNRX": dict(self.SNAP["KNRX"],
                             prevDailyBar={"t": "2026-09-25T04:00:00Z",
                                           "c": 0.312, "h": 0.35})}
        out = parse_snapshots(snap, today=dt.date(2026, 9, 28))["KNRX"]
        assert out["prev_high"] == 0.35

    def test_a_bar_from_today_is_used_as_is(self):
        out = parse_snapshots(self.SNAP, today=dt.date(2026, 9, 28))["KNRX"]
        assert out["prev_close"] == 0.312
        assert out["cum_volume"] == 40_000_000
        assert out["day_high"] == 1.40

    def test_without_a_date_nothing_changes(self):
        """Replay and backtest callers pass no date."""
        out = parse_snapshots(self.SNAP)["KNRX"]
        assert out["prev_close"] == 0.312


class TestYesterdaysMinuteBarIsDropped:
    """Before a symbol's first print today, minuteBar is yesterday's last
    minute. History never checks dates, so it became a completed bar in
    today's VWAP for hours and a candidate swing high for the pullback
    detector. It is judged on its own date: a real pre-market bar from
    today is kept."""

    def snap(self, minute_t):
        return {"KNRX": {
            "latestTrade": {"p": 1.03, "t": "2026-09-29T11:25:00Z"},
            "dailyBar": {"t": "2026-09-28T04:00:00Z", "o": 0.32, "h": 1.40,
                         "l": 0.31, "c": 1.175, "v": 40_000_000},
            "prevDailyBar": {"t": "2026-09-25T04:00:00Z", "c": 0.312},
            "minuteBar": {"t": minute_t, "o": 1.17, "h": 1.18, "l": 1.16,
                          "c": 1.175, "v": 900_000},
        }}

    def test_yesterdays_minute_bar_is_not_passed_on(self):
        raw = self.snap("2026-09-28T23:59:00Z")          # 19:59 ET, 09-28
        out = parse_snapshots(raw, today=dt.date(2026, 9, 29))["KNRX"]
        assert out["minute_bar"] is None

    def test_a_minute_bar_from_today_is_kept(self):
        raw = self.snap("2026-09-29T12:05:00Z")          # 08:05 ET, 09-29
        out = parse_snapshots(raw, today=dt.date(2026, 9, 29))["KNRX"]
        assert out["minute_bar"]["t"] == "2026-09-29T12:05:00Z"

    def test_without_a_date_the_bar_is_kept(self):
        raw = self.snap("2026-09-28T23:59:00Z")
        assert parse_snapshots(raw)["KNRX"]["minute_bar"] is not None
