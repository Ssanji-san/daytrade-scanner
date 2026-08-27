import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.trading.strategy import (bankroll_from, exit_levels, in_window,
                                      should_enter, size_position, split_qty,
                                      weighted_exit)

ET = ZoneInfo("America/New_York")
CFG = Config()


def et(hour, minute):
    return dt.datetime(2026, 7, 14, hour, minute, tzinfo=ET)


def ok_kwargs(**overrides):
    kw = dict(price=3.0, score=0.8, trades_today=0, traded_symbols=set(),
              day_pnl=0.0, now=et(10, 0), cfg=CFG)
    kw.update(overrides)
    return kw


class TestWindow:
    def test_window_edges(self):
        # Opens on the bell and shuts after thirty minutes: the opening
        # first hour is the volatile one this strategy selects for, and
        # entries after it are a different market.
        assert not in_window(et(9, 29), CFG)
        assert in_window(et(9, 30), CFG)
        assert in_window(et(10, 30), CFG)
        assert not in_window(et(10, 31), CFG)

    def test_handles_other_timezones(self):
        utc_10et = et(10, 0).astimezone(dt.timezone.utc)
        assert in_window(utc_10et, CFG)


class TestShouldEnter:
    def test_clean_pass(self):
        take, reasons = should_enter(**ok_kwargs())
        assert take and reasons == []

    @pytest.mark.parametrize("overrides,expected", [
        ({"price": 0.50}, "price"),
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
    def test_the_whole_account_goes_in_at_a_5pct_stop(self):
        # $50 risk / (5% of $5.00) = 200 shares = $1,000, which is also the
        # 100% notional cap. The two agree, so neither silently overrides
        # the other - and the risk is $50 at any share price.
        qty, stop = size_position(5.0, CFG)
        assert qty == 200                      # 1000 / 5.00
        assert stop == pytest.approx(4.75)
        assert (5.0 - stop) * qty == pytest.approx(50.0)

    def test_risk_stays_50_dollars_across_the_price_band(self):
        for price in (1.0, 2.0, 3.0, 5.0):
            qty, stop = size_position(price, CFG)
            assert (price - stop) * qty == pytest.approx(50.0, abs=0.10)
            assert qty * price == pytest.approx(1000.0, abs=2.0)

    def test_the_dollar_ceiling_caps_a_grown_account(self):
        # Position size tracks the balance up to the hard $15,000 ceiling
        # and then stops, however large the account gets.
        cfg = replace(CFG, bot_bankroll=100_000.0)
        qty, _ = size_position(5.0, cfg)
        assert qty * 5.0 == pytest.approx(CFG.bot_max_notional_dollars, abs=5)

    def test_zero_when_price_exceeds_notional_cap(self):
        cfg = replace(CFG, bot_bankroll=1.0)
        qty, _ = size_position(5.0, cfg)
        assert qty == 0


class TestExits:
    def test_stop_and_scale_out_levels(self):
        levels = exit_levels(10.0, CFG)          # 1R = 0.50
        assert levels["stop"] == pytest.approx(9.50)
        assert levels["scale_out"] == pytest.approx(11.00)   # +2R

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


class TestLiveBankroll:
    """Sizing follows the real balance, up and back down again."""

    def test_risk_scales_with_the_account(self):
        for equity, want_risk in ((1_000.0, 50.0), (2_000.0, 100.0),
                                  (5_000.0, 250.0)):
            bank = bankroll_from({"equity": str(equity)}, CFG)
            qty, stop = size_position(3.0, CFG, bankroll=bank)
            assert (3.0 - stop) * qty == pytest.approx(want_risk, abs=1.0)

    def test_it_shrinks_on_a_drawdown_too(self):
        bank = bankroll_from({"equity": "600"}, CFG, last_known=1_000.0)
        qty, stop = size_position(3.0, CFG, bankroll=bank)
        assert (3.0 - stop) * qty == pytest.approx(30.0, abs=1.0)

    def test_the_dollar_ceiling_still_caps_a_large_account(self):
        bank = bankroll_from({"equity": "500000"}, CFG, last_known=400_000.0)
        qty, _ = size_position(3.0, CFG, bankroll=bank)
        assert qty * 3.0 == pytest.approx(CFG.bot_max_notional_dollars, abs=3)

    @pytest.mark.parametrize("account", [
        None, {}, {"equity": None}, {"equity": "not a number"}, {"equity": "0"},
    ])
    def test_an_unreadable_account_falls_back(self, account):
        assert bankroll_from(account, CFG, last_known=1_500.0) == 1_500.0

    def test_an_implausible_jump_is_refused(self):
        # A bad parse must not size the next position. 3x in one read is
        # not a paper account growing, it is a number that went wrong.
        assert bankroll_from({"equity": "90000"}, CFG,
                             last_known=1_000.0) == 1_000.0
        assert bankroll_from({"equity": "2500"}, CFG,
                             last_known=1_000.0) == 2_500.0
