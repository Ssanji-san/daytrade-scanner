import datetime as dt

from scanner.config import Config
from scanner.floats import FloatCache, parse_shares, parse_ticker_map


def test_parse_ticker_map():
    payload = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
               "1": {"cik_str": 1018724, "ticker": "AMZN", "title": "Amazon"}}
    assert parse_ticker_map(payload) == {"AAPL": 320193, "AMZN": 1018724}


def test_parse_shares_takes_latest_by_end_date():
    concept = {"units": {"shares": [
        {"end": "2025-12-31", "val": 15_000_000},
        {"end": "2026-06-30", "val": 18_500_000},
        {"end": "2026-03-31", "val": 16_000_000},
    ]}}
    assert parse_shares(concept) == 18_500_000


def test_parse_shares_handles_empty():
    assert parse_shares({"units": {}}) is None
    assert parse_shares({}) is None


def test_cache_roundtrip_and_staleness(tmp_path):
    cfg = Config(float_cache_path=str(tmp_path / "floats.json"))
    cache = FloatCache(cfg)
    now = dt.datetime(2026, 7, 14, tzinfo=dt.timezone.utc)

    assert cache.get("AAPL") is None
    cache.put("AAPL", 15_000_000, now=now)
    assert cache.get("AAPL") == 15_000_000
    assert not cache.is_stale("AAPL", now=now)

    # reload from disk
    cache2 = FloatCache(cfg)
    assert cache2.get("AAPL") == 15_000_000

    # stale after cfg.float_cache_days
    later = now + dt.timedelta(days=cfg.float_cache_days + 1)
    assert cache2.is_stale("AAPL", now=later)
    assert cache2.is_stale("UNKNOWN", now=now)  # never fetched == stale
