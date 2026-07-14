from scanner.alpaca import (compute_avg_volume, parse_movers, parse_most_actives,
                            parse_news, parse_snapshots)


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
                          "avg_volume": None, "float_shares": None}


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
