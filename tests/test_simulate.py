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


def et(hour, minute):
    return dt.datetime(2026, 8, 12, hour, minute, tzinfo=ET)


def bar(c, h=None, l=None):
    return {"c": c, "h": h if h is not None else c, "l": l if l is not None else c}


@pytest.fixture
def sim(tmp_path):
    journal = Journal(str(tmp_path / "sim.db"), CFG.bot_alert_window_minutes)
    return Simulator(CFG, journal, "2026-08-12", HeuristicScorer(), 0.0)


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

    def test_the_target_banks_half_and_the_runner_trails(self, sim):
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

    def test_the_time_stop_exits_at_the_market(self, sim):
        _open(sim, ts=int(et(10, 0).timestamp()))
        late = et(10, 0) + dt.timedelta(minutes=CFG.bot_time_stop_minutes)
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

    def test_never_more_than_the_concurrency_cap(self, sim):
        sim.enter(et(10, 0), int(et(10, 0).timestamp()), self._rows(10))
        assert len(sim.open) == CFG.bot_max_concurrent_positions

    def test_four_losses_end_the_day(self, sim):
        sim.losses = CFG.bot_max_losses_per_day
        sim.enter(et(10, 0), int(et(10, 0).timestamp()), self._rows(5))
        assert sim.open == {}

    def test_no_entries_outside_the_window(self, sim):
        sim.enter(et(12, 30), int(et(12, 30).timestamp()), self._rows(5))
        assert sim.open == {}

    def test_a_symbol_is_only_entered_once_a_session(self, sim):
        rows = self._rows(1)
        sim.enter(et(10, 0), int(et(10, 0).timestamp()), rows)
        sim.open.clear()                       # pretend it closed
        sim.enter(et(10, 30), int(et(10, 30).timestamp()), rows)
        assert sim.open == {}
