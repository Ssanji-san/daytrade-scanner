import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.state import MarketState

ET = ZoneInfo("America/New_York")
CFG = Config()


def t(hour, minute, second=0):
    return dt.datetime(2026, 7, 14, hour, minute, second, tzinfo=ET)


def snap(price, cum_volume=2_000_000, day_high=None, prev_close=4.0,
         avg_volume=400_000, float_shares=8_000_000):
    return {"price": price, "cum_volume": cum_volume,
            "day_high": day_high if day_high is not None else price,
            "prev_close": prev_close, "avg_volume": avg_volume,
            "float_shares": float_shares,
            "minute_bar": {"t": "2026-07-14T12:45:00Z", "o": price,
                           "h": price, "l": price, "c": price, "v": 5000}}


def feed_flat_then_spike(state):
    """FLAT stays at $10; MOVR spikes 10% over the last 5 minutes."""
    start = t(12, 0)
    for i in range(41):  # 20 min, one sample / 30 s
        now = start + dt.timedelta(seconds=30 * i)
        minutes = 30 * i / 60
        movr = 10.0 if minutes <= 15 else 10.0 * (1 + 0.02 * (minutes - 15))
        state.ingest(now, {"FLAT": snap(10.0), "MOVR": snap(round(movr, 4))})
    return start + dt.timedelta(minutes=20)


def test_planted_mover_tops_five_minute_gainers():
    state = MarketState(CFG)
    now = feed_flat_then_spike(state)
    payload = state.payload(now)
    rows = payload["gainers"]["5"]
    assert rows and rows[0]["symbol"] == "MOVR"
    assert rows[0]["changes"]["5"] == pytest.approx(10.0, abs=1.5)
    assert all(r["symbol"] != "FLAT" for r in rows)


def test_planted_low_float_qualifies_for_hod():
    state = MarketState(CFG)
    now = t(12, 45)  # half the session elapsed -> rvol = 2M / 200k = 10
    state.ingest(now, {"HODX": snap(5.50, day_high=5.55)})
    state.set_news(now, [{"symbol": "HODX", "headline": "FDA approval",
                          "ts": int(now.timestamp()), "url": "u", "source": "bz"}])
    payload = state.payload(now)
    assert [r["symbol"] for r in payload["hod"]["qualified"]] == ["HODX"]
    row = payload["hod"]["qualified"][0]
    assert row["day_pct"] == pytest.approx(37.5)   # 4.00 -> 5.50
    assert row["rvol"] == pytest.approx(10.0)
    assert row["has_news"] is True


def test_news_older_than_max_age_is_not_a_catalyst():
    state = MarketState(CFG)
    now = t(12, 45)
    old = now - dt.timedelta(hours=CFG.news_max_age_hours + 1)
    state.ingest(now, {"HODX": snap(5.50, day_high=5.55)})
    state.set_news(now, [{"symbol": "HODX", "headline": "old news",
                          "ts": int(old.timestamp()), "url": "u", "source": "bz"}])
    row = state.payload(now)["hod"]["qualified"][0]
    assert row["has_news"] is False


def test_stale_banner_after_ingest_gap():
    state = MarketState(CFG)
    now = t(12, 0)
    state.ingest(now, {"FLAT": snap(10.0)})
    assert state.payload(now + dt.timedelta(seconds=2))["stale_since"] is None
    stale = state.payload(now + dt.timedelta(seconds=60))
    assert stale["stale_since"] == int(now.timestamp())
    # last-good data still served
    assert stale["hod"] is not None and "gainers" in stale


def test_remembers_float_and_avg_volume_once_seen():
    state = MarketState(CFG)
    now = t(12, 45)
    state.ingest(now, {"HODX": snap(5.50, day_high=5.55)})
    later = now + dt.timedelta(seconds=30)
    bare = {"price": 5.52, "cum_volume": 2_100_000, "day_high": 5.55,
            "prev_close": 4.0, "avg_volume": None, "float_shares": None}
    state.ingest(later, {"HODX": bare})
    row = state.payload(later)["hod"]["qualified"][0]
    assert row["float_shares"] == 8_000_000
    assert row["rvol"] is not None


def test_calendar_and_news_feed_in_payload():
    state = MarketState(CFG)
    now = t(12, 0)
    state.ingest(now, {"FLAT": snap(10.0)})
    state.set_calendar([{"title": "CPI", "impact": "High", "ts": 1}])
    state.set_news(now, [{"symbol": "FLAT", "headline": "h", "ts": 5, "url": "u",
                          "source": "bz"}])
    payload = state.payload(now)
    assert payload["calendar"][0]["title"] == "CPI"
    assert payload["news"][0]["symbol"] == "FLAT"
