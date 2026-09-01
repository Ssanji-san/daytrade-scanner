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


# --------------------------------------------------------------------------
# A float we cannot fetch is not the same as a company with no float. Unknown
# float is an automatic rejection, so caching a rate-limit as "no float"
# silently removes the stock from the strategy for a week. A fifth of the
# live cache had been poisoned this way.

import asyncio

from scanner.floats import SHARE_CONCEPTS, fetch_shares

CFG = Config()


class _Resp:
    def __init__(self, status, payload=None):
        self.status, self._payload = status, payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Answers each concept URL from a queue of statuses."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.asked = []

    def get(self, url, headers=None):
        self.asked.append(url)
        return self.replies.pop(0)


SHARES = {"units": {"shares": [{"end": "2026-06-30", "val": 12_000_000}]}}


def test_a_successful_fetch_is_answered():
    s = _Session([_Resp(200, SHARES)])
    assert asyncio.run(fetch_shares(s, 320193)) == (12_000_000, True)


def test_a_rate_limit_is_not_an_answer():
    """429 must not be remembered as 'this stock has no float'."""
    for status in (403, 429, 500, 503):
        s = _Session([_Resp(status)])
        assert asyncio.run(fetch_shares(s, 320193)) == (None, False), status


def test_a_network_error_is_not_an_answer():
    class Boom:
        def get(self, url, headers=None):
            raise OSError("connection reset")
    assert asyncio.run(fetch_shares(Boom(), 320193)) == (None, False)


def test_404_everywhere_is_a_real_answer():
    s = _Session([_Resp(404) for _ in SHARE_CONCEPTS])
    assert asyncio.run(fetch_shares(s, 320193)) == (None, True)
    assert len(s.asked) == len(SHARE_CONCEPTS)


def test_it_falls_back_to_the_us_gaap_taxonomy():
    """Issuers that file the us-gaap concept were being thrown away."""
    s = _Session([_Resp(404), _Resp(200, SHARES)])
    assert asyncio.run(fetch_shares(s, 320193)) == (12_000_000, True)
    assert "dei/" in s.asked[0] and "us-gaap/" in s.asked[1]


class TestRetryPolicy:
    def _cache(self, tmp_path):
        from dataclasses import replace
        cfg = replace(CFG, float_cache_path=str(tmp_path / "f.json"))
        return FloatCache(cfg), cfg

    def test_an_unanswered_miss_is_retried_within_the_hour(self, tmp_path):
        cache, cfg = self._cache(tmp_path)
        now = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.timezone.utc)
        cache.put("AIIR", None, now=now, answered=False)
        assert not cache.is_stale("AIIR", now + dt.timedelta(minutes=30))
        assert cache.is_stale("AIIR", now + dt.timedelta(minutes=61))

    def test_a_confirmed_absence_is_remembered_for_days(self, tmp_path):
        cache, cfg = self._cache(tmp_path)
        now = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.timezone.utc)
        cache.put("NOFILE", None, now=now, answered=True)
        assert not cache.is_stale("NOFILE", now + dt.timedelta(hours=6))
        assert cache.is_stale("NOFILE", now + dt.timedelta(days=8))

    def test_legacy_entries_are_retried(self, tmp_path):
        """365 of 1791 cached rows predate the distinction and were poisoned."""
        cache, cfg = self._cache(tmp_path)
        now = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.timezone.utc)
        cache._data["OLD"] = {"shares": None, "fetched": now.isoformat()}
        assert cache.is_stale("OLD", now + dt.timedelta(minutes=61))

    def test_a_real_float_is_still_cached_for_days(self, tmp_path):
        cache, cfg = self._cache(tmp_path)
        now = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.timezone.utc)
        cache.put("CYCU", 25_800_000, now=now, answered=True)
        assert cache.get("CYCU") == 25_800_000
        assert not cache.is_stale("CYCU", now + dt.timedelta(days=3))


class TestACorruptCacheDoesNotKillTheSession:
    """The float cache is read during live_loop's setup, outside its
    try/except - so a half-written file took the whole scanner down with it.
    The file is rewritten in full on every symbol, which a cancelled cloud
    workflow can interrupt.
    """

    def test_unreadable_json_starts_empty_instead_of_raising(self, tmp_path):
        path = tmp_path / "floats.json"
        path.write_text('{"AAPL": {"shares": 15000', encoding="utf-8")
        cfg = Config(float_cache_path=str(path))

        cache = FloatCache(cfg)                    # must not raise

        assert cache.get("AAPL") is None
        assert cache.is_stale("AAPL")              # so it gets refetched

    def test_a_write_leaves_no_partial_file_behind(self, tmp_path):
        cfg = Config(float_cache_path=str(tmp_path / "floats.json"))
        cache = FloatCache(cfg)
        cache.put("AAPL", 15_000_000)
        assert [p.name for p in tmp_path.iterdir()] == ["floats.json"]
