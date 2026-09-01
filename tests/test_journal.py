import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from scanner.trading.journal import Journal

ET = ZoneInfo("America/New_York")


def epoch(hour, minute, second=0):
    return int(dt.datetime(2026, 7, 14, hour, minute, second, tzinfo=ET).timestamp())


FEATURES = {"rvol": 8.0, "day_pct": 25.0, "float_shares": 8e6, "has_news": 1.0}


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path / "journal.db"))


class TestAlerts:
    def test_roundtrip_and_daily_dedupe(self, journal):
        aid = journal.record_alert(epoch(10, 0), "HODX", price=5.00,
                                   r_dollars=0.15, features=FEATURES)
        assert aid is not None
        assert journal.record_alert(epoch(10, 30), "HODX", price=5.20,
                                    r_dollars=0.15, features=FEATURES) is None
        assert journal.record_alert(epoch(10, 30), "OTHR", price=6.0,
                                    r_dollars=0.18, features=FEATURES) is not None

    def test_winner_when_two_r_hit_before_stop(self, journal):
        aid = journal.record_alert(epoch(10, 0), "HODX", price=5.00,
                                   r_dollars=0.15, features=FEATURES)
        journal.track_alert(aid, epoch(10, 2), 5.10)
        journal.track_alert(aid, epoch(10, 8), 5.31)   # >= 5.30 = 2R
        data = journal.labeled_dataset()
        assert len(data) == 1
        assert data[0][1] == 1
        assert data[0][0]["rvol"] == 8.0

    def test_loser_on_stop_first(self, journal):
        aid = journal.record_alert(epoch(10, 0), "HODX", price=5.00,
                                   r_dollars=0.15, features=FEATURES)
        journal.track_alert(aid, epoch(10, 3), 4.84)   # <= 4.85 = stop
        journal.track_alert(aid, epoch(10, 8), 5.40)   # too late, already dead
        assert journal.labeled_dataset()[0][1] == 0

    def test_expires_to_loser_after_30_min(self, journal):
        aid = journal.record_alert(epoch(10, 0), "HODX", price=5.00,
                                   r_dollars=0.15, features=FEATURES)
        journal.track_alert(aid, epoch(10, 15), 5.10)
        assert journal.labeled_dataset() == []          # still open
        journal.track_alert(aid, epoch(10, 31), 5.12)
        assert journal.labeled_dataset()[0][1] == 0


class TestTrades:
    def test_trades_today_and_pnl_survive_reopen(self, journal, tmp_path):
        tid = journal.record_trade_open(epoch(10, 0), "HODX", qty=250,
                                        entry=5.00, stop=4.85, targets=[5.30, 5.45],
                                        features=FEATURES)
        journal.record_trade_close(tid, epoch(10, 12), exit_price=5.30,
                                   exit_reason="target")
        reopened = Journal(str(tmp_path / "journal.db"))
        trades = reopened.trades_today("2026-07-14")
        assert len(trades) == 1
        assert trades[0]["symbol"] == "HODX"
        assert trades[0]["pnl"] == pytest.approx(75.0)          # 0.30 * 250
        assert trades[0]["r_multiple"] == pytest.approx(2.0)
        assert reopened.day_pnl("2026-07-14") == pytest.approx(75.0)
        assert reopened.trades_today("2026-07-15") == []

    def test_open_trade_counts_toward_daily_total(self, journal):
        journal.record_trade_open(epoch(10, 0), "HODX", qty=100, entry=5.0,
                                  stop=4.85, targets=[5.3, 5.45], features=FEATURES)
        assert len(journal.trades_today("2026-07-14")) == 1
        assert journal.day_pnl("2026-07-14") == 0.0

    def test_open_trade_rows_lists_only_unclosed(self, journal):
        tid1 = journal.record_trade_open(epoch(10, 0), "AAA", qty=100, entry=5.0,
                                         stop=4.85, targets=[5.3], features=FEATURES)
        journal.record_trade_open(epoch(10, 5), "BBB", qty=50, entry=6.0,
                                  stop=5.82, targets=[6.36], features=FEATURES)
        journal.record_trade_close(tid1, epoch(10, 20), 5.30, "target")
        open_rows = journal.open_trade_rows()
        assert [r["symbol"] for r in open_rows] == ["BBB"]
        assert open_rows[0]["id"] is not None

    def test_rolling_stats(self, journal):
        for i, (exit_p, reason) in enumerate([(5.30, "target"), (4.85, "stop"),
                                              (5.45, "target"), (4.85, "stop")]):
            tid = journal.record_trade_open(epoch(10, i), f"S{i}", qty=100,
                                            entry=5.00, stop=4.85,
                                            targets=[5.30, 5.45], features=FEATURES)
            journal.record_trade_close(tid, epoch(10, i, 30), exit_p, reason)
        stats = journal.rolling_stats(20)
        assert stats["count"] == 4
        assert stats["win_rate"] == pytest.approx(0.5)
        # R multiples: +2, -1, +3, -1 -> expectancy +0.75R
        assert stats["expectancy_r"] == pytest.approx(0.75)


class TestModelVersions:
    def test_roundtrip_and_latest(self, journal):
        assert journal.latest_model() is None
        journal.record_model(epoch(20, 0), samples=50, holdout_acc=0.62,
                             weights={"bias": 0.1, "rvol": 0.5})
        journal.record_model(epoch(21, 0), samples=80, holdout_acc=0.66,
                             weights={"bias": 0.2, "rvol": 0.6})
        latest = journal.latest_model()
        assert latest["samples"] == 80
        assert latest["weights"]["rvol"] == 0.6
        assert len(journal.model_history()) == 2


def test_wick_through_the_stop_labels_a_loss(tmp_path):
    """Grading on polled last prices alone hides the wick that stopped you out.

    The bar low dipped below the stop and recovered; the poll only ever saw
    the recovered price. That has to grade as a loss or the model learns a
    world it never trades in.
    """
    journal = Journal(str(tmp_path / "j.db"))
    alert_id = journal.record_alert(1_700_000_000, "HODX", price=5.00,
                                    r_dollars=0.15, features={"rvol": 8.0},
                                    setup="micro_pullback")
    journal.track_alert(alert_id, 1_700_000_060, price=5.02,
                        high=5.05, low=4.80)      # wicked well below 4.85
    rows = journal.labeled_dataset()
    assert rows and rows[0][1] == 0


def test_wick_to_target_labels_a_win(tmp_path):
    journal = Journal(str(tmp_path / "j.db"))
    alert_id = journal.record_alert(1_700_000_000, "HODX", price=5.00,
                                    r_dollars=0.15, features={"rvol": 8.0})
    journal.track_alert(alert_id, 1_700_000_060, price=5.10,
                        high=5.35, low=4.95)      # tagged +2R (5.30) intrabar
    assert journal.labeled_dataset()[0][1] == 1


def test_setup_is_stored_and_reported(tmp_path):
    journal = Journal(str(tmp_path / "j.db"))
    journal.record_alert(1_700_000_000, "AAA", 5.0, 0.15, {}, setup="flat_top")
    assert journal.recent_alerts(5)[0]["setup"] == "flat_top"


def test_survives_the_database_file_being_swapped_underneath_it(tmp_path):
    """The cloud workflow commits cache/journal.db mid-session.

    git rewrites the file while the bot holds it open and SQLite then
    reports "attempt to write a readonly database", which used to make the
    bot stop journalling for the rest of the day.
    """
    journal = Journal(str(tmp_path / "j.db"))
    journal.record_alert(1_700_000_000, "AAA", 5.0, 0.15, {"rvol": 8.0})

    journal._db.close()          # what a swapped file looks like to SQLite

    journal.record_alert(1_700_000_100, "BBB", 6.0, 0.18, {"rvol": 9.0})
    assert {a["symbol"] for a in journal.recent_alerts(10)} == {"AAA", "BBB"}


def test_a_real_error_is_not_swallowed_by_the_retry(tmp_path):
    journal = Journal(str(tmp_path / "j.db"))
    with pytest.raises(sqlite3.OperationalError):
        journal._execute("SELECT * FROM does_not_exist_at_all")


class TestGradingHorizon:
    """The label horizon has to match how long the bot actually holds."""

    def test_default_still_times_out_at_thirty_minutes(self, journal):
        aid = journal.record_alert(epoch(10, 0), "HODX", 5.00, 0.15, FEATURES)
        journal.track_alert(aid, epoch(10, 45), 5.02)
        assert journal.labeled_dataset()[0][1] == 0

    def test_a_four_hour_journal_still_grades_a_late_winner(self, tmp_path):
        # 186 of 414 replayed losses were the 30-minute clock expiring, not
        # the stop being hit. At a 4-hour horizon this one is the win it was.
        j = Journal(str(tmp_path / "long.db"), alert_window_minutes=240)
        aid = j.record_alert(epoch(10, 0), "HODX", 5.00, 0.15, FEATURES)
        j.track_alert(aid, epoch(11, 0), 5.05)          # still open at 60 min
        assert j.labeled_dataset() == []
        j.track_alert(aid, epoch(13, 20), 5.31)         # +2R at minute 200
        assert j.labeled_dataset()[0][1] == 1

    def test_the_horizon_still_expires(self, tmp_path):
        j = Journal(str(tmp_path / "long.db"), alert_window_minutes=240)
        aid = j.record_alert(epoch(10, 0), "HODX", 5.00, 0.15, FEATURES)
        j.track_alert(aid, epoch(14, 30), 5.02)         # 4h30m, nothing hit
        assert j.labeled_dataset()[0][1] == 0


class TestExcursionsAfterTheLabel:
    """A winner's mfe used to freeze the minute it crossed +2R.

    That filed every gradual runner as exactly 2R, so the model had no way
    to tell a scratch from a monster - the one thing the runner exit exists
    to capture.
    """

    def test_mfe_keeps_recording_past_the_target(self, journal):
        aid = journal.record_alert(epoch(10, 0), "HODX", 5.00, 0.15, FEATURES)
        journal.track_alert(aid, epoch(10, 5), 5.31)          # +2R, label 1
        journal.track_alert(aid, epoch(10, 20), 6.50)         # ran to +10R
        row = journal.recent_alerts(1)[0]
        assert row["label"] == 1
        assert row["mfe"] == pytest.approx(1.50)              # 10R, not 2R

    def test_the_label_never_changes_once_decided(self, journal):
        aid = journal.record_alert(epoch(10, 0), "HODX", 5.00, 0.15, FEATURES)
        journal.track_alert(aid, epoch(10, 5), 5.31)          # win
        journal.track_alert(aid, epoch(10, 30), 4.00)         # then collapsed
        row = journal.recent_alerts(1)[0]
        assert row["label"] == 1
        assert row["mae"] == pytest.approx(-1.00)             # recorded anyway


class TestLossesToday:
    """The day's kill switch is a count of closed losers."""

    def _closed(self, journal, symbol, r_multiple):
        tid = journal.record_trade_open(epoch(10, 0), symbol, qty=50,
                                        entry=5.0, stop=4.0, targets=[7.0],
                                        features=FEATURES)
        journal._execute("UPDATE trades SET exit_ts=?, exit_price=?, pnl=?,"
                         " r_multiple=? WHERE id=?",
                         (epoch(11, 0), 4.0, r_multiple * 50, r_multiple, tid))
        journal._commit()

    def test_counts_only_closed_losers(self, journal):
        day = "2026-07-14"
        assert journal.losses_today(day) == 0
        self._closed(journal, "AAA", -1.0)
        self._closed(journal, "BBB", 2.0)
        self._closed(journal, "CCC", -1.0)
        assert journal.losses_today(day) == 2

    def test_an_open_losing_trade_is_not_counted_yet(self, journal):
        journal.record_trade_open(epoch(10, 0), "OPEN", qty=50, entry=5.0,
                                  stop=4.0, targets=[7.0], features=FEATURES)
        assert journal.losses_today("2026-07-14") == 0


class TestNearMissUpgrade:
    """A symbol almost always shows up near before it qualifies.

    The row is UNIQUE(day, symbol), so the near miss was written first and
    the later qualifying alert was dropped - which is how a journal with six
    real trades in it came to hold nothing but observed=1 rows, and would
    have trained the model entirely on setups the bot never buys.
    """

    def test_a_qualifying_alert_upgrades_the_near_miss(self, journal):
        near = journal.record_alert(epoch(9, 40), "HODX", 5.00, 0.25,
                                    FEATURES, setup=None, observed=1)
        upgraded = journal.record_alert(epoch(9, 55), "HODX", 5.20, 0.26,
                                        FEATURES, setup="micro_pullback",
                                        observed=0)
        assert upgraded == near             # same row, still being graded
        row = journal.recent_alerts(5)[0]
        assert row["observed"] == 0
        assert row["setup"] == "micro_pullback"
        assert row["price"] == 5.00         # as first spotted, not re-priced
        assert journal.learning_progress(40)["tradable"] == 0   # not graded yet

    def test_a_near_miss_never_demotes_a_tradable_row(self, journal):
        journal.record_alert(epoch(9, 40), "HODX", 5.00, 0.25, FEATURES,
                             setup="flat_top", observed=0)
        assert journal.record_alert(epoch(9, 55), "HODX", 5.20, 0.26,
                                    FEATURES, observed=1) is None
        assert journal.recent_alerts(5)[0]["observed"] == 0

    def test_the_upgraded_row_counts_as_tradable_once_graded(self, journal):
        aid = journal.record_alert(epoch(10, 0), "HODX", 5.00, 0.15,
                                   FEATURES, observed=1)
        journal.record_alert(epoch(10, 5), "HODX", 5.05, 0.15, FEATURES,
                             observed=0)
        journal.track_alert(aid, epoch(10, 40), 5.02)     # times out, label 0
        assert journal.learning_progress(40) == {"labeled": 1, "tradable": 1,
                                                 "needed": 40}


class TestCentTargetGrading:
    """The label has to be the trade the bot takes.

    Grading on +2R while the bot banks at +20c measured a move it never
    waits for: 2R against the flat 5% stop is a +10% move, and the position
    is 65% gone at 20c. The model was being fit to an outcome that never
    happened.
    """

    def _journal(self, tmp_path):
        return Journal(str(tmp_path / "cents.db"), alert_window_minutes=10,
                       win_target_cents=0.20)

    def test_twenty_cents_wins_where_two_r_would_not_have(self, tmp_path):
        j = self._journal(tmp_path)
        # r_dollars 0.25 = a 5% stop on a $5 stock, so +2R is $5.50.
        aid = j.record_alert(epoch(10, 0), "HODX", 5.00, 0.25, FEATURES)
        j.track_alert(aid, epoch(10, 2), price=5.21, high=5.22, low=5.05)
        row = j.recent_alerts(1)[0]
        assert row["label"] == 1
        assert j.outcome_rows()[0]["resolved_r"] == pytest.approx(0.8)

    def test_the_same_target_is_worth_more_on_a_cheap_stock(self, tmp_path):
        j = self._journal(tmp_path)
        # r_dollars 0.05 = a 5% stop on a $1 stock: 20c is 4R down here.
        aid = j.record_alert(epoch(10, 0), "PENY", 1.00, 0.05, FEATURES)
        j.track_alert(aid, epoch(10, 2), price=1.21, high=1.22, low=1.05)
        assert j.outcome_rows()[0]["resolved_r"] == pytest.approx(4.0)

    def test_the_stop_still_takes_precedence(self, tmp_path):
        j = self._journal(tmp_path)
        aid = j.record_alert(epoch(10, 0), "HODX", 5.00, 0.25, FEATURES)
        j.track_alert(aid, epoch(10, 2), price=5.21, high=5.25, low=4.70)
        assert j.recent_alerts(1)[0]["label"] == 0
        assert j.outcome_rows()[0]["resolved_r"] == pytest.approx(-1.0)

    def test_a_move_short_of_the_target_still_times_out(self, tmp_path):
        j = self._journal(tmp_path)
        aid = j.record_alert(epoch(10, 0), "HODX", 5.00, 0.25, FEATURES)
        j.track_alert(aid, epoch(10, 3), price=5.15, high=5.18, low=5.02)
        assert j.labeled_dataset() == []                 # still open
        j.track_alert(aid, epoch(10, 12), price=5.10, high=5.12, low=5.05)
        assert j.recent_alerts(1)[0]["label"] == 0

    def test_without_a_cent_target_the_r_rule_stands(self, tmp_path):
        j = Journal(str(tmp_path / "r.db"), alert_window_minutes=10)
        aid = j.record_alert(epoch(10, 0), "HODX", 5.00, 0.25, FEATURES)
        j.track_alert(aid, epoch(10, 2), price=5.21, high=5.22, low=5.05)
        assert j.labeled_dataset() == []                 # 20c is not +2R
