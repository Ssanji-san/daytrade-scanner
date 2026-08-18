"""Pure decision core of the bot loop (I/O-free parts)."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.trading.bot import choose_entries, features_from_row
from scanner.trading.model import HeuristicScorer

from .fixtures import make_state

ET = ZoneInfo("America/New_York")
CFG = Config()
OPEN = dt.datetime(2026, 7, 14, 9, 30, tzinfo=ET)


def et(hour, minute):
    return dt.datetime(2026, 7, 14, hour, minute, tzinfo=ET)


def row(symbol="HODX", price=5.50, rvol=8.0, **kw):
    state = make_state(symbol=symbol, price=price, rvol=rvol,
                       day_high=kw.pop("day_high", price), **kw)
    state["dist_from_hod"] = 0.0
    state["failed"] = []
    return state


class TestFeatures:
    def test_extracts_model_features(self):
        f = features_from_row(row(), now=et(10, 15))
        assert f["rvol"] == 8.0
        assert f["day_pct"] == 25.0
        assert f["float_shares"] == 8_000_000
        assert f["has_news"] == 1.0
        assert f["change_5"] == 3.0
        assert f["minutes_since_open"] == pytest.approx(45.0)


class TestChooseEntries:
    def test_takes_best_scored_within_remaining_cap(self):
        rows = [row("AAA", rvol=6.0), row("BBB", rvol=14.0), row("CCC", rvol=9.0)]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=3,
                               traded_symbols=set(), day_pnl=0.0,
                               now=et(10, 0), cfg=CFG)
        assert [p["symbol"] for p in picks] == ["BBB"]   # 1 slot left, best rvol
        assert picks[0]["qty"] > 0
        assert picks[0]["stop"] < picks[0]["price"]

    def test_cap_never_exceeded_in_one_cycle(self):
        rows = [row(f"S{i}") for i in range(8)]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=0,
                               traded_symbols=set(), day_pnl=0.0,
                               now=et(10, 0), cfg=CFG)
        assert len(picks) == CFG.bot_max_trades_per_day

    def test_skips_traded_low_score_and_out_of_band(self):
        rows = [row("DUP"), row("CHEAP", price=1.50, day_high=1.50),
                row("WEAK", rvol=0.5, catalyst=None), row("GOOD")]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=0,
                               traded_symbols={"DUP"}, day_pnl=0.0,
                               now=et(10, 0), cfg=CFG)
        assert [p["symbol"] for p in picks] == ["GOOD"]

    def test_nothing_outside_window_or_after_kill_switch(self):
        rows = [row()]
        assert choose_entries(rows, HeuristicScorer(), 0, set(), 0.0,
                              et(9, 20), CFG) == []
        assert choose_entries(rows, HeuristicScorer(), 0, set(), -200.0,
                              et(10, 0), CFG) == []
