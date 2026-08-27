import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.trading.strategy import (exit_levels, in_window, should_enter,
                                      size_position, split_qty, weighted_exit)

ET = ZoneInfo("America/New_York")
CFG = Config()


def et(hour, minute):
    return dt.datetime(2026, 7, 14, hour, minute, tzinfo=ET)


def ok_kwargs(**overrides):
    kw = dict(price=5.0, score=0.8, trades_today=0, traded_symbols=set(),
              day_pnl=0.0, now=et(10, 0), cfg=CFG)
    kw.update(overrides)
    return kw


class TestWindow:
    def test_window_edges(self):
        # Opens on the bell: the first five minutes are often the best move
        # of the day on a gapper, and the range break lives in that slot.
        assert not in_window(et(9, 29), CFG)
        assert in_window(et(9, 30), CFG)
        assert in_window(et(11, 30), CFG)
        assert not in_window(et(11, 31), CFG)

    def test_handles_other_timezones(self):
        utc_10et = et(10, 0).astimezone(dt.timezone.utc)
        assert in_window(utc_10et, CFG)


class TestShouldEnter:
    def test_clean_pass(self):
        take, reasons = should_enter(**ok_kwargs())
        assert take and reasons == []

    @pytest.mark.parametrize("overrides,expected", [
        ({"price": 1.50}, "price"),
        ({"price": 25.0}, "price"),
        ({"now": et(9, 20)}, "window"),
        ({"now": et(12, 0)}, "window"),
        ({"trades_today": 4}, "daily_cap"),
        ({"traded_symbols": {"HODX"}, "symbol": "HODX"}, "already_traded"),
        ({"score": 0.30}, "score"),
        ({"day_pnl": -150.0}, "kill_switch"),
    ])
    def test_rejections(self, overrides, expected):
        symbol = overrides.pop("symbol", "TEST")
        take, reasons = should_enter(symbol=symbol, **ok_kwargs(**overrides))
        assert not take
        assert expected in reasons

    def test_default_symbol_not_blocked_by_other_traded(self):
        take, _ = should_enter(**ok_kwargs(traded_symbols={"OTHER"}))
        assert take


class TestSizing:
    def test_notional_cap_binds_with_default_config(self):
        # risk $10 at a 3% stop implies $666 notional -> capped at 25% of $1k = $250
        qty, stop = size_position(5.0, CFG)
        assert qty == 50                       # 250 / 5.00
        assert stop == pytest.approx(4.85)

    def test_uncapped_when_risk_is_small(self):
        cfg = replace(CFG, bot_risk_pct=0.5)   # risk $5 -> 16 sh, under the $250 cap
        qty, stop = size_position(10.0, cfg)
        assert qty == 16                       # 5 / 0.30
        assert stop == pytest.approx(9.70)

    def test_zero_when_price_exceeds_notional_cap(self):
        cfg = replace(CFG, bot_bankroll=10.0)
        qty, _ = size_position(5.0, cfg)
        assert qty == 0


class TestExits:
    def test_stop_and_scale_out_levels(self):
        levels = exit_levels(10.0, CFG)          # 1R = 0.30
        assert levels["stop"] == pytest.approx(9.70)
        assert levels["scale_out"] == pytest.approx(10.60)   # +2R

    def test_split_qty(self):
        assert split_qty(9) == (5, 4)
        assert split_qty(250) == (125, 125)
        assert split_qty(1) == (1, 0)


class TestWeightedExit:
    def test_share_weighted_average(self):
        assert weighted_exit([(25, 5.30), (25, 5.60)]) == pytest.approx(5.45)

    def test_none_when_no_shares(self):
        assert weighted_exit([]) is None


class TestScoreThresholdOverride:
    def test_explicit_threshold_wins_over_config(self):
        take, reasons = should_enter(**ok_kwargs(score=0.30),
                                     score_threshold=0.25)
        assert take and reasons == []

    def test_config_bar_applies_when_no_override(self):
        take, reasons = should_enter(**ok_kwargs(score=0.30))
        assert not take and "score" in reasons
