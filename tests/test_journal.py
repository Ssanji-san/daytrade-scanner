import datetime as dt
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
