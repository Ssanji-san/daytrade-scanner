"""The trade simulator: what the bot would actually have done.

These pin the exit paths, because every P&L number this project reports now
comes out of them.
"""
import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.backtest.simulate import Position, Simulator
from scanner.trading.journal import Journal
from scanner.trading.model import HeuristicScorer

ET = ZoneInfo("America/New_York")
CFG = Config()
# The live config scalps. These exercise the swing exits - scale at +2R,
# trail the runner - which stay reachable by configuration.
SWING = replace(CFG, bot_runner_mode=False, bot_time_stop_minutes=20)


def et(hour, minute):
    return dt.datetime(2026, 8, 12, hour, minute, tzinfo=ET)


def bar(c, h=None, l=None):
    return {"c": c, "h": h if h is not None else c, "l": l if l is not None else c}


@pytest.fixture
def sim(tmp_path):
    journal = Journal(str(tmp_path / "sim.db"), CFG.bot_alert_window_minutes)
    return Simulator(CFG, journal, "2026-08-12", HeuristicScorer(), 0.0)


@pytest.fixture
def swing(tmp_path):
    journal = Journal(str(tmp_path / "swing.db"), CFG.bot_alert_window_minutes)
    return Simulator(SWING, journal, "2026-08-12", HeuristicScorer(), 0.0)


def _open(sim, price=5.00, ts=None):
    """Put one position on the books without going through choose_entries."""
    ts = ts or int(et(10, 0).timestamp())
    levels = {"stop": round(price * 0.8, 2),
              "scale_out": round(price * 1.4, 2)}
    pick = {"symbol": "HODX", "price": price, "qty": 50,
            "features": {}, "setup": "micro_pullback", "stop": levels["stop"]}
    tid = sim.journal.record_trade_open(ts, "HODX", qty=50, entry=price,
                                        stop=levels["stop"],
                                        targets=[levels["scale_out"]],
                                        features={}, setup="micro_pullback")
    pos = Position(tid, pick, levels, ts)
    sim.open["HODX"] = pos
    sim.traded.add("HODX")
    return pos


class TestExits:
    def test_a_stop_out_loses_exactly_one_r(self, sim):
        _open(sim)
        sim.manage(et(10, 5), int(et(10, 5).timestamp()),
                   {"HODX": bar(4.10, h=5.05, l=3.90)})
        trade = sim.journal.all_trades()[0]
        assert trade["exit_reason"] == "stop"
        assert trade["r_multiple"] == pytest.approx(-1.0)

    def test_a_bar_touching_both_is_scored_as_the_stop(self, sim):
        # Intrabar order is unknowable from OHLC. Assuming the target came
        # first is how a backtest promises what it cannot pay.
        _open(sim)
        sim.manage(et(10, 5), int(et(10, 5).timestamp()),
                   {"HODX": bar(5.00, h=7.50, l=3.90)})
        assert sim.journal.all_trades()[0]["exit_reason"] == "stop"

    def test_the_target_banks_half_and_the_runner_trails(self, swing):
        sim = swing
        pos = _open(sim)
        sim.manage(et(10, 5), int(et(10, 5).timestamp()),
                   {"HODX": bar(7.10, h=7.20, l=5.00)})
        assert pos.banked and "HODX" in sim.open      # runner still riding
        assert pos.legs == [(25, 7.00)]               # half banked at +2R

        # Runs to 9.00, then gives back more than the 5% trail.
        sim.manage(et(10, 6), int(et(10, 6).timestamp()),
                   {"HODX": bar(8.40, h=9.00, l=8.40)})
        sim.manage(et(10, 7), int(et(10, 7).timestamp()),
                   {"HODX": bar(8.30, h=8.60, l=8.00)})
        trade = sim.journal.all_trades()[0]
        assert trade["exit_reason"] == "trailing"
        assert trade["r_multiple"] > 2.0              # the runner paid

    def test_the_time_stop_exits_at_the_market(self, swing):
        sim = swing
        _open(sim, ts=int(et(10, 0).timestamp()))
        late = et(10, 0) + dt.timedelta(minutes=SWING.bot_time_stop_minutes)
        sim.manage(late, int(late.timestamp()), {"HODX": bar(5.10)})
        trade = sim.journal.all_trades()[0]
        assert trade["exit_reason"] == "time_stop"
        assert trade["r_multiple"] == pytest.approx(0.1)   # 0.10 on a 1.00 R

    def test_everything_is_flat_by_the_bell(self, sim):
        _open(sim)
        sim.manage(et(15, 50), int(et(15, 50).timestamp()),
                   {"HODX": bar(5.40)})
        assert sim.journal.all_trades()[0]["exit_reason"] == "flatten"
        assert not sim.open


class TestCaps:
    def _rows(self, n):
        return [{"symbol": f"S{i}", "price": 5.0, "rvol": 9.0, "day_pct": 22.0,
                 "float_shares": 8e6, "has_news": True, "dist_from_hod": 0.0,
                 "day_high": 5.0, "changes": {"5": 3.0}, "above_vwap": True,
                 "catalyst": {"score": 0.8}, "day_volume": 500_000,
                 "avg_volume": 100_000, "gap_pct": 0.0,
                 "setup": {"setup": "micro_pullback", "stop": 4.85}}
                for i in range(n)]

    def test_the_account_runs_out_before_the_concurrency_cap(self, sim):
        """$2,500 buys two $1,000 positions and a $500 slice, then stops."""
        sim.enter(et(10, 0), int(et(10, 0).timestamp()), self._rows(10))
        assert len(sim.open) == 3
        spent = sum(p.qty * p.entry for p in sim.open.values())
        assert spent <= CFG.bot_bankroll
        assert spent == pytest.approx(CFG.bot_bankroll, abs=15)

    def test_never_more_than_the_concurrency_cap(self, sim, tmp_path):
        """With capital to spare, the ceiling is what binds."""
        cfg = replace(CFG, bot_bankroll=100_000.0)
        j = Journal(str(tmp_path / "big.db"), cfg.bot_alert_window_minutes)
        rich = Simulator(cfg, j, "2026-08-12", HeuristicScorer(), 0.0)
        rich.enter(et(10, 0), int(et(10, 0).timestamp()), self._rows(10))
        assert len(rich.open) == cfg.bot_max_concurrent_positions

    def test_four_losses_end_the_day(self, sim):
        sim.losses = CFG.bot_max_losses_per_day
        sim.enter(et(10, 0), int(et(10, 0).timestamp()), self._rows(5))
        assert sim.open == {}

    def test_no_entries_outside_the_window(self, sim):
        sim.enter(et(12, 31), int(et(12, 31).timestamp()), self._rows(5))
        assert sim.open == {}

    def test_a_symbol_is_only_entered_once_a_session(self, sim):
        rows = self._rows(1)
        sim.enter(et(10, 0), int(et(10, 0).timestamp()), rows)
        sim.open.clear()                       # pretend it closed
        sim.enter(et(10, 30), int(et(10, 30).timestamp()), rows)
        assert sim.open == {}


class TestTradeReport:
    """The report is the only place trade results are ever seen.

    It ran a whole 8-month simulation and then died on a missing import,
    throwing the results away - so it gets executed in the tests.
    """

    def test_reports_without_blowing_up(self, sim, capsys):
        ts = int(et(10, 0).timestamp())
        for i, (exit_price, reason) in enumerate(
                [(4.00, "stop"), (7.00, "target"), (5.10, "time_stop")]):
            tid = sim.journal.record_trade_open(
                ts + i, f"S{i}", qty=50, entry=5.00, stop=4.00,
                targets=[7.00], features={}, setup="micro_pullback")
            sim.journal.record_trade_close(tid, ts + 600, exit_price, reason)
        from scripts.backtest import trade_report
        trade_report(sim.journal, CFG)
        out = capsys.readouterr().out
        assert "3 trades" in out
        assert "expectancy" in out
        assert "of notional" in out          # the spread warning survives

    def test_says_so_when_nothing_traded(self, sim, capsys):
        from scripts.backtest import trade_report
        trade_report(sim.journal, CFG)
        assert "no trades were taken" in capsys.readouterr().out


def _scalp_open(sim, price=3.00, ts=None, cfg=None):
    """Open a scalp the way the entry path would, at $1,000 notional."""
    from scanner.trading.strategy import scalp_levels, size_position
    cfg = cfg or sim.cfg
    ts = ts or int(et(9, 40).timestamp())
    qty, _ = size_position(price, cfg)
    levels = scalp_levels(price, cfg)
    pick = {"symbol": "HODX", "price": price, "qty": qty, "features": {},
            "setup": "micro_pullback"}
    tid = sim.journal.record_trade_open(ts, "HODX", qty=qty, entry=price,
                                        stop=levels["stop"],
                                        targets=[levels["target"]],
                                        features={}, setup="micro_pullback")
    pos = Position(tid, pick, levels, ts, cfg)
    sim.open["HODX"] = pos
    sim.traded.add("HODX")
    return pos


def candle(o, c, h=None, l=None):
    return {"o": o, "c": c, "h": h if h is not None else max(o, c),
            "l": l if l is not None else min(o, c)}


class TestScalpExits:
    """Fixed-cent target, 5% stop, out the moment it stalls."""

    def test_the_stop_is_50_dollars_at_any_price(self, sim):
        for price in (1.0, 2.0, 3.0, 5.0):
            pos = _scalp_open(sim, price=price)
            assert (pos.entry - pos.stop) * pos.qty == pytest.approx(50, abs=1)
            sim.open.clear()
            sim.traded.clear()

    def test_scaling_out_banks_the_majority_and_runs_the_rest(self, sim):
        pos = _scalp_open(sim, price=3.00)          # 333 sh, target 3.20
        assert (pos.bank_qty, pos.runner_qty) == (216, 117)   # 65 / 35
        sim.manage(et(9, 42), int(et(9, 42).timestamp()),
                   {"HODX": candle(3.05, 3.18, h=3.25, l=3.04)})
        assert pos.banked and "HODX" in sim.open
        assert pos.legs == [(216, 3.20)]
        # The trade can no longer lose: the stop came up to entry.
        assert pos.stop == pytest.approx(pos.entry)

    def test_the_runner_trails_and_keeps_part_of_the_run(self, sim):
        """The point of the trail: a reversal no longer costs the whole run.

        A fixed break-even stop handed back every cent above entry the moment
        price came off. The trail rides up behind the high, so the runner
        keeps what it made instead of scratching.
        """
        pos = _scalp_open(sim, price=3.00)
        sim.manage(et(9, 42), int(et(9, 42).timestamp()),
                   {"HODX": candle(3.05, 3.18, h=3.25, l=3.04)})
        assert pos.trail_pct == pytest.approx(5.0)      # full width down here
        sim.manage(et(9, 44), int(et(9, 44).timestamp()),
                   {"HODX": candle(3.10, 2.95, h=3.12, l=2.90)})

        trade = sim.journal.all_trades()[0]
        assert trade["exit_reason"] == "trailing"
        # 5% under the 3.25 high, not the 3.00 entry - and well clear of the
        # 2.90 the bar actually traded down to.
        assert pos.legs[-1] == (117, pytest.approx(3.09))
        assert trade["pnl"] > 0

    def test_the_runner_can_never_book_below_the_entry(self, sim):
        """At $5 a flat 5% trail would sit under the entry. It is capped."""
        pos = _scalp_open(sim, price=5.00)
        sim.manage(et(9, 42), int(et(9, 42).timestamp()),
                   {"HODX": candle(5.05, 5.18, h=5.20, l=5.04)})
        assert pos.trail_pct == pytest.approx(3.84)     # capped, not 5%
        sim.manage(et(9, 44), int(et(9, 44).timestamp()),
                   {"HODX": candle(5.10, 4.60, h=5.12, l=4.55)})

        assert pos.legs[-1][1] >= pos.entry             # break-even at worst
        assert sim.journal.all_trades()[0]["r_multiple"] > 0

    def test_a_banked_runner_is_exempt_from_the_clock(self, sim):
        """The clock is for a position that has not paid yet."""
        open_ts = int(et(9, 40).timestamp())
        _scalp_open(sim, price=3.00, ts=open_ts)
        sim.manage(et(9, 41), int(et(9, 41).timestamp()),
                   {"HODX": candle(3.05, 3.18, h=3.25, l=3.04)})
        late = et(9, 40) + dt.timedelta(minutes=CFG.bot_time_stop_minutes + 5)
        sim.manage(late, int(late.timestamp()),
                   {"HODX": candle(3.30, 3.40, h=3.45, l=3.35)})
        assert "HODX" in sim.open                       # still riding

    def test_the_fixed_break_even_stop_is_still_reachable(self, sim, tmp_path):
        cfg = replace(CFG, bot_runner_uses_trail=False)
        j = Journal(str(tmp_path / "be.db"), cfg.bot_alert_window_minutes)
        flat = Simulator(cfg, j, "2026-08-12", HeuristicScorer(), 0.0)
        pos = _scalp_open(flat, price=3.00, cfg=cfg)
        flat.manage(et(9, 42), int(et(9, 42).timestamp()),
                    {"HODX": candle(3.05, 3.18, h=3.25, l=3.04)})
        assert pos.trail_pct is None
        flat.manage(et(9, 44), int(et(9, 44).timestamp()),
                    {"HODX": candle(3.10, 2.95, h=3.12, l=2.90)})
        assert j.all_trades()[0]["exit_reason"] == "breakeven"

    def test_taking_the_whole_position_at_the_target(self, sim, tmp_path):
        cfg = replace(CFG, bot_bank_pct=100.0)
        j = Journal(str(tmp_path / "full.db"), cfg.bot_alert_window_minutes)
        full = Simulator(cfg, j, "2026-08-12", HeuristicScorer(), 0.0)
        _scalp_open(full, price=3.00, cfg=cfg)
        full.manage(et(9, 42), int(et(9, 42).timestamp()),
                    {"HODX": candle(3.05, 3.18, h=3.25, l=3.04)})
        trade = j.all_trades()[0]
        assert trade["exit_reason"] == "target"
        assert trade["exit_price"] == pytest.approx(3.20)

    def test_two_dojis_are_a_stall_and_one_is_not(self, sim):
        _scalp_open(sim, price=3.00)
        flat = candle(3.05, 3.051, h=3.09, l=3.02)      # tiny body, wide range
        sim.manage(et(9, 41), int(et(9, 41).timestamp()), {"HODX": flat})
        assert "HODX" in sim.open                        # one is not enough
        sim.manage(et(9, 42), int(et(9, 42).timestamp()), {"HODX": flat})
        assert sim.journal.all_trades()[0]["exit_reason"] == "stall"

    def test_a_real_candle_resets_the_stall_count(self, sim):
        pos = _scalp_open(sim, price=3.00)
        flat = candle(3.05, 3.051, h=3.09, l=3.02)
        drive = candle(3.05, 3.12, h=3.13, l=3.04)       # decisive body
        sim.manage(et(9, 41), int(et(9, 41).timestamp()), {"HODX": flat})
        assert pos.dojis == 1
        sim.manage(et(9, 42), int(et(9, 42).timestamp()), {"HODX": drive})
        assert pos.dojis == 0
        sim.manage(et(9, 43), int(et(9, 43).timestamp()), {"HODX": flat})
        assert "HODX" in sim.open                        # count restarted

    def test_the_stop_still_takes_precedence_over_the_target(self, sim):
        _scalp_open(sim, price=3.00)
        sim.manage(et(9, 42), int(et(9, 42).timestamp()),
                   {"HODX": candle(3.00, 3.00, h=3.30, l=2.80)})
        trade = sim.journal.all_trades()[0]
        assert trade["exit_reason"] == "stop"
        assert trade["r_multiple"] == pytest.approx(-1.0, abs=0.02)

    def test_ten_minutes_is_the_maximum_hold_before_it_pays(self, sim):
        open_ts = int(et(9, 40).timestamp())
        _scalp_open(sim, price=3.00, ts=open_ts)
        late = et(9, 40) + dt.timedelta(minutes=CFG.bot_time_stop_minutes)
        sim.manage(late, int(late.timestamp()),
                   {"HODX": candle(3.04, 3.05, h=3.06, l=3.03)})
        assert sim.journal.all_trades()[0]["exit_reason"] == "time_stop"
