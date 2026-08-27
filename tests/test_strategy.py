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
        # Opens on the bell and shuts after thirty minutes: the opening
        # drive is what this strategy selects for, and entries after it are
        # a different market.
        assert not in_window(et(9, 29), CFG)
        assert in_window(et(9, 30), CFG)
        assert in_window(et(10, 0), CFG)
        assert not in_window(et(10, 1), CFG)

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
        ({"trades_today": 10}, "daily_cap"),
        ({"losses_today": 4}, "loss_cap"),
        ({"open_positions": 4}, "concurrency"),
        ({"traded_symbols": {"HODX"}, "symbol": "HODX"}, "already_traded"),
        ({"score": 0.30}, "score"),
    ])
    def test_rejections(self, overrides, expected):
        symbol = overrides.pop("symbol", "TEST")
        take, reasons = should_enter(symbol=symbol, **ok_kwargs(**overrides))
        assert not take
        assert expected in reasons

    def test_default_symbol_not_blocked_by_other_traded(self):
        take, _ = should_enter(**ok_kwargs(traded_symbols={"OTHER"}))
        assert take

    def test_three_losses_still_trades_the_fourth_blocks(self):
        take, _ = should_enter(**ok_kwargs(losses_today=3))
        assert take
        take, reasons = should_enter(**ok_kwargs(losses_today=4))
        assert not take and "loss_cap" in reasons

    def test_daily_cap_is_ten_not_four(self):
        take, _ = should_enter(**ok_kwargs(trades_today=9))
        assert take


class TestDollarKillSwitch:
    """Disabled by default: the day ends on a loss count, not an amount."""

    def test_inert_at_zero(self):
        take, reasons = should_enter(**ok_kwargs(day_pnl=-900.0))
        assert take and "kill_switch" not in reasons

    def test_applies_when_configured(self):
        cfg = replace(CFG, bot_daily_loss_pct=3.0)
        take, reasons = should_enter(**ok_kwargs(day_pnl=-150.0, cfg=cfg))
        assert not take and "kill_switch" in reasons


class TestBrokerState:
    """Read what the account reports; never re-implement the margin rules."""

    def test_no_snapshot_fails_open(self):
        take, _ = should_enter(**ok_kwargs(account=None, notional=250))
        assert take

    def test_missing_buying_power_fails_open(self):
        take, _ = should_enter(**ok_kwargs(account={"equity": "1000"},
                                           notional=250))
        assert take

    def test_blocked_account_stops_entries(self):
        take, reasons = should_enter(
            **ok_kwargs(account={"trading_blocked": True}, notional=250))
        assert not take and "broker_blocked" in reasons

    def test_insufficient_buying_power(self):
        take, reasons = should_enter(
            **ok_kwargs(account={"buying_power": "100"}, notional=250))
        assert not take and "buying_power" in reasons
        take, _ = should_enter(
            **ok_kwargs(account={"buying_power": "1000"}, notional=250))
        assert take

    def test_flagged_account_uses_its_day_trading_power(self):
        # A restricted account still reports cash buying power; the broker's
        # own day-trading figure is the binding one. Reading it beats
        # hardcoding a rule that is mid-rollout across the industry.
        take, reasons = should_enter(**ok_kwargs(
            account={"buying_power": "5000", "pattern_day_trader": True,
                     "daytrading_buying_power": "0"}, notional=250))
        assert not take and "buying_power" in reasons


class TestSizing:
    def test_position_is_250_dollars_at_a_20pct_stop(self):
        # $50 risk / (20% of $5.00) = 50 shares = $250, which is also the
        # 25% notional cap. The two formulas agree, so neither one
        # silently overrides the other.
        qty, stop = size_position(5.0, CFG)
        assert qty == 50                       # 250 / 5.00
        assert stop == pytest.approx(4.00)

    def test_uncapped_when_risk_is_small(self):
        cfg = replace(CFG, bot_risk_pct=0.5)   # risk $5 -> 2 sh, under the $250 cap
        qty, stop = size_position(10.0, cfg)
        assert qty == 2                        # 5 / 2.00
        assert stop == pytest.approx(8.00)

    def test_zero_when_price_exceeds_notional_cap(self):
        cfg = replace(CFG, bot_bankroll=10.0)
        qty, _ = size_position(5.0, cfg)
        assert qty == 0


class TestExits:
    def test_stop_and_scale_out_levels(self):
        levels = exit_levels(10.0, CFG)          # 1R = 2.00
        assert levels["stop"] == pytest.approx(8.00)
        assert levels["scale_out"] == pytest.approx(14.00)   # +2R

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
