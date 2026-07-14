import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.history import SymbolHistory, rvol

ET = ZoneInfo("America/New_York")
CFG = Config()


def t(hour, minute, second=0):
    """An aware ET timestamp on a fixed trading day."""
    return dt.datetime(2026, 7, 14, hour, minute, second, tzinfo=ET)


def fill(history, start, minutes, price_fn, vol_fn=lambda i: 1000 * i):
    """Add one sample per minute for `minutes` minutes starting at `start`."""
    for i in range(minutes + 1):
        history.add(start + dt.timedelta(minutes=i), price_fn(i), vol_fn(i))


class TestNMinuteChange:
    def test_five_minute_change(self):
        h = SymbolHistory()
        # price climbs 10 cents/min from $10.00
        fill(h, t(10, 0), 20, lambda i: 10.0 + 0.10 * i)
        now = t(10, 20)
        # 5 min ago price was 11.50, now 12.00 -> +4.348%
        assert h.n_minute_change(now, 5) == pytest.approx(100 * (12.0 - 11.5) / 11.5)

    def test_uses_sample_at_or_before_target(self):
        h = SymbolHistory()
        h.add(t(10, 0), 10.0, 0)
        h.add(t(10, 4), 11.0, 0)   # nothing exactly at now-5min; 10:00 is the base
        h.add(t(10, 6), 12.0, 0)
        assert h.n_minute_change(t(10, 6), 5) == pytest.approx(20.0)

    def test_insufficient_history_returns_none(self):
        h = SymbolHistory()
        fill(h, t(10, 0), 3, lambda i: 10.0)
        assert h.n_minute_change(t(10, 3), 5) is None

    def test_empty_returns_none(self):
        assert SymbolHistory().n_minute_change(t(10, 0), 5) is None

    def test_ring_buffer_trims_old_samples(self):
        h = SymbolHistory(maxlen=10)
        fill(h, t(10, 0), 30, lambda i: 10.0 + i)
        assert len(h) == 10
        # oldest retained sample is 10:21 -> 25-min lookback impossible
        assert h.n_minute_change(t(10, 30), 25) is None
        assert h.n_minute_change(t(10, 30), 5) is not None


class TestRvol:
    def test_half_session_half_volume_is_one(self):
        # 9:30-16:00 ET session = 390 min; half elapsed at 12:45
        assert rvol(500_000, 1_000_000, t(12, 45), CFG) == pytest.approx(1.0)

    def test_five_times_average_pace(self):
        assert rvol(2_500_000, 1_000_000, t(12, 45), CFG) == pytest.approx(5.0)

    def test_at_open_uses_floor_fraction(self):
        # elapsed ~0 -> expected volume floored at 5% of daily average
        assert rvol(100_000, 1_000_000, t(9, 30), CFG) == pytest.approx(
            100_000 / (1_000_000 * CFG.rvol_min_session_fraction))

    def test_premarket_uses_floor_fraction(self):
        assert rvol(50_000, 1_000_000, t(8, 0), CFG) == pytest.approx(1.0)

    def test_after_close_uses_full_day(self):
        assert rvol(2_000_000, 1_000_000, t(17, 30), CFG) == pytest.approx(2.0)

    def test_no_baseline_returns_none(self):
        assert rvol(100_000, 0, t(12, 0), CFG) is None
        assert rvol(100_000, None, t(12, 0), CFG) is None
