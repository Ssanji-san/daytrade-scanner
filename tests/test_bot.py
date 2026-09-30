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


def row(symbol="HODX", price=3.00, rvol=8.0, **kw):
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

    def test_records_the_room_to_each_ceiling(self):
        room = {"room_prev_high": 0.10, "room_premarket_high": None,
                "room_half_dollar": 0.45}
        f = features_from_row(row(resistance=room), now=et(10, 15))
        assert f["room_prev_high"] == 0.10
        assert f["room_premarket_high"] is None     # no ceiling, not zero
        assert f["room_half_dollar"] == 0.45

    def test_room_is_none_when_the_row_has_none(self):
        f = features_from_row(row(), now=et(10, 15))
        assert f["room_half_dollar"] is None


class TestChooseEntries:
    def test_takes_best_scored_within_remaining_cap(self):
        rows = [row("AAA", rvol=6.0), row("BBB", rvol=14.0), row("CCC", rvol=9.0)]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=9,
                               traded_symbols=set(), day_pnl=0.0,
                               now=et(10, 0), cfg=CFG)
        assert [p["symbol"] for p in picks] == ["BBB"]   # 1 slot left, best rvol
        assert picks[0]["qty"] > 0
        assert picks[0]["stop"] < picks[0]["price"]

    def test_concurrency_caps_one_cycle_below_the_daily_cap(self):
        # The daily cap is 10, but the whole account goes into a single
        # trade - so what bounds one pass is the account, not the day.
        rows = [row(f"S{i}") for i in range(8)]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=0,
                               traded_symbols=set(), day_pnl=0.0,
                               now=et(10, 0), cfg=CFG)
        assert len(picks) == CFG.bot_max_concurrent_positions

    def test_already_open_positions_count_against_concurrency(self):
        rows = [row(f"S{i}") for i in range(8)]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=0,
                               traded_symbols=set(), day_pnl=0.0,
                               now=et(10, 0), cfg=CFG,
                               open_positions=CFG.bot_max_concurrent_positions)
        assert picks == []

    def test_daily_cap_still_binds(self):
        rows = [row(f"S{i}") for i in range(8)]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=10,
                               traded_symbols=set(), day_pnl=0.0,
                               now=et(10, 0), cfg=CFG)
        assert picks == []

    def test_skips_traded_low_score_and_out_of_band(self):
        rows = [row("DUP"), row("PRICEY", price=25.0, day_high=25.0),
                row("WEAK", rvol=0.5, catalyst=None), row("GOOD")]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=0,
                               traded_symbols={"DUP"}, day_pnl=0.0,
                               now=et(10, 0), cfg=CFG)
        assert [p["symbol"] for p in picks] == ["GOOD"]

    def test_nothing_outside_the_window(self):
        rows = [row()]
        assert choose_entries(rows, HeuristicScorer(), 0, set(), 0.0,
                              et(7, 50), CFG) == []     # before IEX opens
        assert choose_entries(rows, HeuristicScorer(), 0, set(), 0.0,
                              et(10, 1), CFG) == []     # after the close

    def test_four_losses_ends_the_day(self):
        # The kill switch is a loss count now, not a dollar figure, so a
        # big paper drawdown alone no longer stops entries.
        rows = [row()]
        assert choose_entries(rows, HeuristicScorer(), 0, set(), -200.0,
                              et(10, 0), CFG) != []
        assert choose_entries(rows, HeuristicScorer(), 0, set(), 0.0,
                              et(10, 0), CFG, losses_today=4) == []


class TestSkipsAreExplained:
    """Every one of these reasons was already computed and thrown away.

    The alert is journalled and tracked to its outcome whether or not the
    bot buys it, so the reason it passed is the half that was missing from
    "which rule blocked a winner".
    """

    def test_the_daily_cap_names_itself(self):
        skips = []
        rows = [row("AAA"), row("BBB")]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=10,
                               traded_symbols=set(), day_pnl=0.0,
                               now=et(10, 0), cfg=CFG, skips=skips)
        assert picks == []
        assert {s["reason"] for s in skips} == {"daily_cap"}
        assert {s["symbol"] for s in skips} == {"AAA", "BBB"}

    def test_a_row_with_no_trigger_yet_is_not_a_refusal(self):
        """It is a different thing from a setup the bot turned down, and
        the journal must be able to tell them apart."""
        skips = []
        no_trigger = row("AAA")
        no_trigger["setup"] = None
        choose_entries([no_trigger], HeuristicScorer(), 0, set(), 0.0,
                       et(10, 0), CFG, skips=skips)
        assert [s["reason"] for s in skips] == ["no_setup"]

    def test_several_reasons_are_reported_together(self):
        skips = []
        choose_entries([row("DUP")], HeuristicScorer(), trades_today=10,
                       traded_symbols={"DUP"}, day_pnl=0.0, now=et(10, 0),
                       cfg=CFG, skips=skips)
        reason = skips[0]["reason"]
        assert "daily_cap" in reason and "already_traded" in reason

    def test_the_score_that_lost_is_kept_with_it(self):
        skips = []
        choose_entries([row("WEAK", rvol=0.5, catalyst=None)],
                       HeuristicScorer(), 0, set(), 0.0, et(10, 0), CFG,
                       skips=skips)
        assert skips and skips[0]["score"] is not None

    def test_taken_rows_are_not_reported_as_skips(self):
        skips = []
        picks = choose_entries([row("GOOD")], HeuristicScorer(), 0, set(),
                               0.0, et(10, 0), CFG, skips=skips)
        assert [p["symbol"] for p in picks] == ["GOOD"]
        assert skips == []

    def test_asking_for_nothing_still_works(self):
        """The backtest and every existing caller pass no list at all."""
        assert choose_entries([row("GOOD")], HeuristicScorer(), 0, set(),
                              0.0, et(10, 0), CFG)


class TestTopGainerOnly:
    """Ross: focus on the number one leading gainer."""

    def test_off_by_default(self):
        assert CFG.bot_top_gainer_only is False

    def test_only_the_biggest_gainer_is_considered(self):
        from dataclasses import replace
        cfg = replace(CFG, bot_top_gainer_only=True)
        rows = [row("AAA", day_pct=40.0, rvol=20.0),
                row("BBB", day_pct=90.0, rvol=6.0),
                row("CCC", day_pct=15.0, rvol=30.0)]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=0,
                               traded_symbols=set(), day_pnl=0.0,
                               now=et(9, 45), cfg=cfg)
        assert [p["symbol"] for p in picks] == ["BBB"]

    def test_the_leader_blocked_means_no_trade_not_the_runner_up(self):
        from dataclasses import replace
        cfg = replace(CFG, bot_top_gainer_only=True)
        rows = [row("AAA", day_pct=40.0), row("BBB", day_pct=90.0)]
        picks = choose_entries(rows, HeuristicScorer(), trades_today=0,
                               traded_symbols={"BBB"}, day_pnl=0.0,
                               now=et(9, 45), cfg=cfg)
        assert picks == []
