import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.trading.strategy import (bankroll_from, exit_levels, in_window,
                                      position_slots, runner_trail_pct,
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
        # Opens on the bell, shuts three hours later. Both edges are
        # inclusive, so 12:30 still admits an entry and 12:31 does not.
        assert not in_window(et(9, 29), CFG)
        assert in_window(et(9, 30), CFG)
        assert in_window(et(10, 31), CFG)   # was the old close
        assert in_window(et(12, 30), CFG)
        assert not in_window(et(12, 31), CFG)

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
        ({"now": et(12, 31)}, "window"),
        ({"trades_today": 10}, "daily_cap"),
        ({"losses_today": 4}, "loss_cap"),
        ({"open_positions": 5}, "concurrency"),
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
    def test_one_unit_goes_in_at_a_5pct_stop(self):
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

    def test_the_dollar_ceiling_caps_an_oversized_unit(self):
        cfg = replace(CFG, bot_position_dollars=100_000.0)
        qty, _ = size_position(5.0, cfg)
        assert qty * 5.0 == pytest.approx(CFG.bot_max_notional_dollars, abs=5)

    def test_zero_when_price_exceeds_notional_cap(self):
        cfg = replace(CFG, bot_position_dollars=1.0)
        qty, _ = size_position(5.0, cfg)
        assert qty == 0


class TestBudget:
    """The last slice of an account takes a part-sized position.

    Without a budget the third trade on a $2,473 balance asked for a full
    $1,000, and the buying-power check refused it outright - so the account
    sat two-thirds deployed with cash it would not use.
    """

    def test_a_budget_smaller_than_the_unit_shrinks_the_position(self):
        qty, stop = size_position(3.0, CFG, budget=473.74)
        assert qty == 157                        # 473.74 / 3.00
        assert qty * 3.0 == pytest.approx(471.0, abs=3.0)
        assert (3.0 - stop) * qty == pytest.approx(23.6, abs=0.5)

    def test_a_budget_larger_than_the_unit_does_not_inflate_it(self):
        qty, _ = size_position(3.0, CFG, budget=50_000.0)
        assert qty * 3.0 == pytest.approx(CFG.bot_position_dollars, abs=3.0)

    def test_a_slice_too_small_to_bother_with_is_refused(self):
        assert size_position(3.0, CFG, budget=149.0)[0] == 0
        assert size_position(3.0, CFG, budget=151.0)[0] > 0

    def test_spending_an_account_down(self):
        """$2,473.74 buys $1,000 + $1,000 + $473, and then nothing."""
        budget, opened = 2_473.74, []
        for _ in range(5):
            qty, _ = size_position(3.0, CFG, budget=budget)
            if qty < 1:
                break
            opened.append(qty * 3.0)
            budget -= qty * 3.0
        assert len(opened) == 3
        assert opened[0] == pytest.approx(999.0)
        assert opened[1] == pytest.approx(999.0)
        assert opened[2] == pytest.approx(474.0)


class TestPositionSlots:
    def test_the_balance_decides_how_many_fit(self):
        assert position_slots(2_473.74, CFG) == 3      # 1k + 1k + 473
        assert position_slots(1_000.0, CFG) == 1
        assert position_slots(0.0, CFG) == 0

    def test_a_remainder_under_the_floor_does_not_count(self):
        assert position_slots(1_100.0, CFG) == 1       # the $100 is unusable
        assert position_slots(1_150.0, CFG) == 2

    def test_the_ceiling_bounds_a_grown_account(self):
        assert position_slots(5_000.0, CFG) == 5
        assert position_slots(50_000.0, CFG) == CFG.bot_max_concurrent_positions


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
    """The real balance decides how many positions fit, not how big.

    Position size is a fixed unit - $50 of risk, whatever the account holds.
    Growth buys more slots rather than fatter trades, so one bad name can
    never cost more than it did yesterday.
    """

    def test_the_account_buys_slots_not_bigger_trades(self):
        for equity, want_slots in ((1_000.0, 1), (2_473.74, 3), (5_000.0, 5)):
            bank = bankroll_from({"equity": str(equity)}, CFG)
            assert bank == pytest.approx(equity)
            assert position_slots(bank, CFG) == want_slots
            qty, stop = size_position(3.0, CFG)
            assert (3.0 - stop) * qty == pytest.approx(50.0, abs=1.0)

    def test_it_shrinks_on_a_drawdown_too(self):
        bank = bankroll_from({"equity": "600"}, CFG, last_known=1_000.0)
        assert bank == pytest.approx(600.0)
        assert position_slots(bank, CFG) == 1

    def test_a_first_reading_is_taken_as_the_baseline(self):
        """The 3x guard must not measure the first read against the seed.

        It used to: a $4,000 account tripped it against the $1,000 config
        seed on every cycle and stayed pinned at $1,000 for the session.
        """
        assert bankroll_from({"equity": "4000"}, CFG,
                             last_known=None) == 4_000.0

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


class TestRunnerTrail:
    """Once the bulk is banked, the runner rides a trail that can only rise.

    The width is capped so the first stop never sits below what was paid -
    a flat 5% trail on a $5 entry would put it at $4.94, turning a banked
    winner back into a loser and defeating the entire point of scaling out.
    """

    def test_a_cheap_runner_gets_the_full_width(self):
        # $2.20 on a $2.00 entry: 5% is 11c, which still clears the entry.
        assert runner_trail_pct(2.00, 2.20, CFG) == pytest.approx(5.0)

    def test_an_expensive_runner_is_capped_at_break_even(self):
        pct = runner_trail_pct(5.00, 5.20, CFG)
        assert pct == pytest.approx(3.84)               # not the full 5%
        assert 5.20 * (1 - pct / 100) >= 5.00           # never below entry

    def test_the_cap_floors_rather_than_rounds(self):
        """Rounding up widens the trail past break-even, which is the one
        thing the cap exists to prevent."""
        pct = runner_trail_pct(5.00, 5.20, CFG)
        assert pct == 3.84                              # 3.846..., not 3.85

    def test_the_trail_rises_with_the_price(self):
        """Further from the entry, the cap stops binding."""
        assert runner_trail_pct(5.00, 5.20, CFG) < runner_trail_pct(5.00, 6.00, CFG)

    def test_no_trail_when_the_price_is_not_above_the_entry(self):
        assert runner_trail_pct(5.00, 5.00, CFG) is None
        assert runner_trail_pct(5.00, 4.90, CFG) is None
        assert runner_trail_pct(0, 5.00, CFG) is None
