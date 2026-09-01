import asyncio

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
