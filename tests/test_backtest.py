"""The replay must never see the future.

A backtest that peeks looks brilliant and loses money live, so the
point-in-time rules get tested harder than anything else here.
"""
import datetime as dt

import pytest

from scanner.backtest import fetch, replay
from scanner.config import Config
from scanner.trading.journal import Journal

CFG = Config()


def bar(t, o, h, l, c, v=20_000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


class TestNoLookahead:
    def test_unpublished_headlines_are_invisible(self):
        items = [{"symbol": "AAA", "headline": "already out", "ts": 1_000},
                 {"symbol": "AAA", "headline": "not yet", "ts": 5_000}]
        visible = replay.visible_news(items, now_ts=2_000)
        assert [i["headline"] for i in visible] == ["already out"]

    def test_a_headline_exactly_now_counts(self):
        items = [{"symbol": "AAA", "headline": "just broke", "ts": 2_000}]
        assert len(replay.visible_news(items, now_ts=2_000)) == 1

    def test_missing_timestamps_are_dropped_not_assumed(self):
        assert replay.visible_news([{"headline": "no ts"}], now_ts=9_999) == []

    def test_volume_baseline_excludes_the_simulated_day(self):
        """Today's own volume is what rvol is meant to be measured against."""
        rows = [bar("2026-08-10T00:00:00Z", 1, 1, 1, 1, v=100),
                bar("2026-08-11T00:00:00Z", 1, 1, 1, 1, v=200),
                bar("2026-08-12T00:00:00Z", 1, 1, 1, 1, v=999_999)]
        assert fetch.prior_avg_volume(rows, "2026-08-12", 30) == 150

    def test_no_baseline_before_the_first_session(self):
        rows = [bar("2026-08-12T00:00:00Z", 1, 1, 1, 1, v=500)]
        assert fetch.prior_avg_volume(rows, "2026-08-12", 30) is None


class TestSessionCursor:
    """Cumulative volume and high-of-day are built up, never read off the end."""

    def test_totals_accumulate_minute_by_minute(self):
        cursor = replay.SessionCursor()
        first = cursor.snapshot("AAA", bar("t1", 5, 5.5, 4.9, 5.2, v=1_000),
                                prev_close=4.0, avg_volume=10_000,
                                float_shares=8e6)
        assert first["cum_volume"] == 1_000
        assert first["day_high"] == 5.5

        second = cursor.snapshot("AAA", bar("t2", 5.2, 6.0, 5.1, 5.9, v=2_500),
                                 prev_close=4.0, avg_volume=10_000,
                                 float_shares=8e6)
        assert second["cum_volume"] == 3_500      # not 2_500
        assert second["day_high"] == 6.0

    def test_day_high_never_falls_back(self):
        cursor = replay.SessionCursor()
        cursor.snapshot("AAA", bar("t1", 5, 9.0, 4.9, 8.0), 4.0, 1e4, 8e6)
        after = cursor.snapshot("AAA", bar("t2", 8, 8.2, 7.0, 7.1), 4.0, 1e4, 8e6)
        assert after["day_high"] == 9.0

    def test_the_snapshot_matches_what_ingest_expects(self):
        snap = replay.SessionCursor().snapshot(
            "AAA", bar("t1", 5, 5.5, 4.9, 5.2), 4.0, 10_000, 8e6)
        assert set(snap) >= {"price", "cum_volume", "day_high", "prev_close",
                             "avg_volume", "float_shares", "minute_bar"}
        assert snap["minute_bar"]["h"] == 5.5


class TestTimeline:
    def test_minutes_come_out_in_order(self):
        rows = {"AAA": [bar("2026-08-12T13:31:00Z", 1, 1, 1, 1),
                        bar("2026-08-12T13:30:00Z", 1, 1, 1, 1)],
                "BBB": [bar("2026-08-12T13:30:00Z", 2, 2, 2, 2)]}
        timeline = replay.bars_by_minute(rows)
        assert list(timeline) == ["2026-08-12T13:30:00Z",
                                  "2026-08-12T13:31:00Z"]
        assert set(timeline["2026-08-12T13:30:00Z"]) == {"AAA", "BBB"}

    def test_bars_without_a_close_are_skipped(self):
        rows = {"AAA": [{"t": "2026-08-12T13:30:00Z", "h": 1, "l": 1}]}
        assert replay.bars_by_minute(rows) == {}


class TestCandidateSelection:
    def test_keeps_a_real_mover_in_the_price_band(self):
        daily = {"MOVR": [bar("2026-08-11T00:00:00Z", 5, 5, 5, 5.00),
                          bar("2026-08-12T00:00:00Z", 6, 7, 6, 7.00)]}
        assert fetch.select_candidates(daily, CFG) == {"2026-08-12": ["MOVR"]}

    def test_drops_a_quiet_day_and_an_out_of_band_price(self):
        daily = {"FLAT": [bar("2026-08-11T00:00:00Z", 5, 5, 5, 5.00),
                          bar("2026-08-12T00:00:00Z", 5, 5, 5, 5.05)],
                 "PRICEY": [bar("2026-08-11T00:00:00Z", 100, 100, 100, 100.0),
                            bar("2026-08-12T00:00:00Z", 150, 150, 150, 150.0)]}
        assert fetch.select_candidates(daily, CFG) == {}

    def test_first_session_has_no_baseline_so_is_never_a_candidate(self):
        daily = {"AAA": [bar("2026-08-12T00:00:00Z", 5, 5, 5, 9.0)]}
        assert fetch.select_candidates(daily, CFG) == {}


def test_replay_journals_graded_alerts_without_touching_live(tmp_path):
    """End to end on one synthetic session, through the real pipeline."""
    journal = Journal(str(tmp_path / "backtest.db"))
    day = "2026-08-12"

    # A low-float mover with a fresh catalyst, ramping through the open.
    minute_rows = []
    for i in range(12):
        stamp = f"{day}T13:{30 + i:02d}:00Z"
        price = 5.00 + i * 0.10
        minute_rows.append(bar(stamp, price, price + 0.02,
                               round(price * 0.995, 4), price, v=40_000))
    minute_bars = {"MOVR": minute_rows}
    news = [{"symbol": "MOVR", "headline": "MOVR receives FDA approval",
             "ts": int(dt.datetime.fromisoformat(
                 f"{day}T13:00:00+00:00").timestamp()),
             "url": "u", "source": "bz"}]
    context = {"prev_close": {"MOVR": 4.00},
               "avg_volume": {"MOVR": 400_000},
               "float_shares": {"MOVR": 8_000_000}}

    graded = replay.replay_day(day, minute_bars, news, context, journal, CFG)

    assert graded > 0
    alerts = journal.recent_alerts(20)
    assert any(a["symbol"] == "MOVR" for a in alerts)
    assert str(tmp_path) in journal.path          # never the live journal


def test_a_symbol_without_a_previous_close_is_skipped(tmp_path):
    """Every percentage is measured against the prior close - no baseline,
    no honest reading."""
    journal = Journal(str(tmp_path / "backtest.db"))
    minute_bars = {"NEW": [bar("2026-08-12T13:30:00Z", 5, 5.1, 4.9, 5.05)]}
    graded = replay.replay_day("2026-08-12", minute_bars, [],
                               {"prev_close": {}}, journal, CFG)
    assert graded == 0
    assert journal.recent_alerts(5) == []
