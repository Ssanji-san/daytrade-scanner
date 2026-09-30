"""The replay must never see the future.

A backtest that peeks looks brilliant and loses money live, so the
point-in-time rules get tested harder than anything else here.
"""
import datetime as dt

import pytest

from scanner.backtest import fetch, replay
from scanner.config import Config
from scanner.trading.journal import Journal

# The 500K real-volume floor needs a SIP tape these tests do not model;
# it has its own tests (test_volume, test_hod, and the SIP replay test).
CFG = Config(hod_min_real_volume=0)


def bar(t, o, h, l, c, v=20_000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def pullback_bars(day, start_minute=30, v=60_000):
    """Bars that actually form a micro-pullback, so a trigger appears.

    An alert is only graded once it has one - see
    Journal.tracking_alerts - so a fixture of flat or monotonically rising
    bars is never labelled, which is correct and not what these tests are
    about. Rise to a swing high, pull back inside the configured depth
    band, then break it.
    """
    shape = [(4.50, 4.55, 4.48, 4.55),
             (4.55, 4.70, 4.53, 4.70),      # swing high 4.70
             (4.70, 4.66, 4.60, 4.62),      # pullback: 2.1% deep, trigger 4.66
             (4.62, 4.80, 4.62, 4.78)]      # breaks it -> micro_pullback
    return [bar(f"{day}T13:{start_minute + i:02d}:00Z", o, h, l, c, v=v)
            for i, (o, h, l, c) in enumerate(shape)]


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


class TestPrevHigh:
    """Yesterday's high reaches the replay the way the live snapshot has it."""

    def test_cursor_carries_it(self):
        snap = replay.SessionCursor().snapshot(
            "AAA", bar("t1", 5, 5.5, 4.9, 5.2), 4.0, 10_000, 8e6,
            prev_high=5.8)
        assert snap["prev_high"] == 5.8

    def test_context_takes_it_from_the_day_before(self):
        from scripts.backtest import _context_for
        daily = {"AAA": [bar("2026-08-10T04:00:00Z", 1, 1.5, 1, 1.2),
                         bar("2026-08-11T04:00:00Z", 1, 1.9, 1, 1.4),
                         bar("2026-08-12T04:00:00Z", 1, 9.9, 1, 9.0)]}
        context = _context_for("2026-08-12", daily, {}, CFG)
        assert context["prev_high"]["AAA"] == 1.9     # not today's 9.9
        assert context["prev_close"]["AAA"] == 1.4

    def test_context_carries_the_country(self):
        from scripts.backtest import _context_for
        daily = {"AAA": [bar("2026-08-11T04:00:00Z", 1, 1.9, 1, 1.4)]}
        context = _context_for("2026-08-12", daily, {}, CFG,
                               countries={"AAA": "China"})
        assert context["country"]["AAA"] == "China"


def _replay_country(tmp_path, country):
    import sqlite3
    journal = Journal(str(tmp_path / "backtest.db"))
    day = "2026-08-12"
    minute_rows = []
    for i in range(12):
        stamp = f"{day}T13:{30 + i:02d}:00Z"
        price = 3.00 + i * 0.06
        minute_rows.append(bar(stamp, price, price + 0.02,
                               round(price * 0.995, 4), price, v=40_000))
    context = {"prev_close": {"SOS": 2.40}, "country": {"SOS": country},
               "avg_volume": {"SOS": 400_000},
               "float_shares": {"SOS": 8_000_000}}
    replay.replay_day(day, {"SOS": minute_rows}, [], context, journal, CFG)
    return [r[0] or "" for r in sqlite3.connect(journal.path).execute(
        "SELECT failed FROM alerts WHERE symbol='SOS'")]


def test_replay_knows_a_chinese_company(tmp_path):
    """The replay has to apply the same news rule as the live scan."""
    failed = _replay_country(tmp_path, "China")
    assert failed and all("china_news" in f for f in failed)


def test_replay_knows_a_us_company(tmp_path):
    failed = _replay_country(tmp_path, "DE")
    assert failed and not any("china_news" in f for f in failed)


def _replay_sip(tmp_path, sip_v):
    """MOVR ramps 09:30-09:41 with 40K IEX shares a minute; SIP from 04:00."""
    import sqlite3
    journal = Journal(str(tmp_path / "backtest.db"))
    day = "2026-08-12"
    minute_rows = []
    for i in range(12):
        stamp = f"{day}T13:{30 + i:02d}:00Z"
        price = 3.00 + i * 0.06
        minute_rows.append(bar(stamp, price, price + 0.02,
                               round(price * 0.995, 4), price, v=40_000))
    sip = [bar(f"{day}T12:{m:02d}:00Z", 3, 3, 3, 3, v=sip_v)
           for m in range(0, 60)]                      # 08:00-08:59 ET
    context = {"prev_close": {"MOVR": 2.40}, "country": {"MOVR": "DE"},
               "avg_volume": {"MOVR": 400_000},
               "float_shares": {"MOVR": 8_000_000}}
    replay.replay_day(day, {"MOVR": minute_rows}, [], context, journal,
                      Config(), sip_bars={"MOVR": sip})
    return [r[0] or "" for r in sqlite3.connect(journal.path).execute(
        "SELECT failed FROM alerts WHERE symbol='MOVR'")]


def test_replay_counts_real_volume_from_sip(tmp_path):
    """60 x 10K pre-market SIP bars = 600K, over the 500K floor."""
    failed = _replay_sip(tmp_path, 10_000)
    assert failed and not any("real_volume" in f for f in failed)


def test_replay_fails_a_thin_tape(tmp_path):
    """Nothing on SIP before the open, and at most 12 x 40K = 480K of IEX
    since the cutoff: under 500K all session."""
    failed = _replay_sip(tmp_path, 0)
    assert failed and any("real_volume" in f for f in failed)


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
        daily = {"MOVR": [bar("2026-08-11T00:00:00Z", 2, 2, 2, 2.00),
                          bar("2026-08-12T00:00:00Z", 2.4, 3.2, 2.4, 3.00)]}
        assert fetch.select_candidates(daily, CFG) == {"2026-08-12": ["MOVR"]}

    def test_a_spike_that_faded_is_still_a_candidate(self):
        """The live screener sees it while it is running, not at the close.

        Selecting on close-to-close would silently drop the days this
        scanner exists to catch - ran 40%, gave it all back.
        """
        daily = {"FADE": [bar("2026-08-11T00:00:00Z", 5, 5, 5, 5.00),
                          bar("2026-08-12T00:00:00Z", 5, 7.0, 4.9, 5.10)]}
        assert fetch.select_candidates(daily, CFG) == {"2026-08-12": ["FADE"]}

    def test_drops_a_quiet_day_and_an_out_of_band_price(self):
        daily = {"FLAT": [bar("2026-08-11T00:00:00Z", 5, 5, 5, 5.00),
                          bar("2026-08-12T00:00:00Z", 5, 5.05, 4.95, 5.05)],
                 "PRICEY": [bar("2026-08-11T00:00:00Z", 100, 100, 100, 100.0),
                            bar("2026-08-12T00:00:00Z", 150, 150, 149, 150.0)]}
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


def test_replay_hands_yesterdays_high_to_the_alert(tmp_path):
    """The live snapshot carries prev_high; the replay has to as well, or
    the backtest measures resistance the bot never sees - or none at all."""
    import json
    import sqlite3
    journal = Journal(str(tmp_path / "backtest.db"))
    day = "2026-08-12"
    minute_rows = []
    for i in range(12):
        stamp = f"{day}T13:{30 + i:02d}:00Z"
        price = 5.00 + i * 0.10
        minute_rows.append(bar(stamp, price, price + 0.02,
                               round(price * 0.995, 4), price, v=40_000))
    context = {"prev_close": {"MOVR": 4.00}, "prev_high": {"MOVR": 9.00},
               "avg_volume": {"MOVR": 400_000},
               "float_shares": {"MOVR": 8_000_000}}
    replay.replay_day(day, {"MOVR": minute_rows}, [], context, journal, CFG)

    rows = sqlite3.connect(journal.path).execute(
        "SELECT features FROM alerts WHERE symbol='MOVR'").fetchall()
    assert rows
    for (features,) in rows:
        assert json.loads(features)["room_prev_high"] > 0


def test_a_symbol_without_a_previous_close_is_skipped(tmp_path):
    """Every percentage is measured against the prior close - no baseline,
    no honest reading."""
    journal = Journal(str(tmp_path / "backtest.db"))
    minute_bars = {"NEW": [bar("2026-08-12T13:30:00Z", 5, 5.1, 4.9, 5.05)]}
    graded = replay.replay_day("2026-08-12", minute_bars, [],
                               {"prev_close": {}}, journal, CFG)
    assert graded == 0
    assert journal.recent_alerts(5) == []


class TestSymbolFilter:
    def test_keeps_common_stock(self):
        assert fetch.tradable_symbols(["AAPL", "F", "MOVR"]) == ["AAPL", "F", "MOVR"]

    def test_drops_preferreds_warrants_and_units(self):
        """These break the bars endpoint and are not this strategy's trade."""
        messy = ["AAPL", "ABR-PD", "ACHR-WT", "AAC-UN", "AGM-A"]
        assert fetch.tradable_symbols(messy) == ["AAPL"]


def test_a_thin_symbol_still_gets_resolved_at_the_close(tmp_path):
    """A symbol that stops printing must not leave an unlabeled alert.

    Unlabeled alerts teach the model nothing, and a setup that never
    reached +2R in the session did not work - that is a loss, not missing
    data, once the 30-minute window has passed.
    """
    journal = Journal(str(tmp_path / "backtest.db"))
    day = "2026-08-12"
    rows = pullback_bars(day)                # triggers, then goes quiet
    rows.append(bar(f"{day}T15:00:00Z", 4.78, 4.80, 4.76, 4.78, v=60_000))
    context = {"prev_close": {"THIN": 4.00},
               "avg_volume": {"THIN": 400_000},
               "float_shares": {"THIN": 8_000_000}}
    news = [{"symbol": "THIN", "headline": "THIN receives FDA approval",
             "ts": int(dt.datetime.fromisoformat(
                 f"{day}T13:00:00+00:00").timestamp())}]

    replay.replay_day(day, {"THIN": rows}, news, context, journal, CFG)

    alerts = [a for a in journal.recent_alerts(10) if a["symbol"] == "THIN"]
    assert alerts, "the setup should have been journalled"
    assert alerts[0]["label"] is not None, "flat for 90 minutes is a loss"


class TestSessionWindow:
    """Two windows, not one.

    New alerts are only recorded while the live bot could still enter
    (07:30-12:15 ET), but bars keep flowing to the flatten time so a
    four-hour hold can be graded on what actually happened. Cutting
    bars at the entry cutoff would mark every late trade a timeout,
    which is the artifact the longer horizon exists to remove.
    """

    def test_bars_run_to_the_flatten_time(self):
        # 19:00Z = 15:00 ET: past the 12:15 entry cutoff, but the bot
        # could still be holding, so the bar is kept for grading.
        rows = {"AAA": [bar("2026-08-12T13:35:00Z", 1, 1, 1, 1),   # 09:35 ET
                        bar("2026-08-12T19:00:00Z", 1, 1, 1, 1)]}
        timeline = replay.bars_by_minute(rows, CFG)
        assert list(timeline) == ["2026-08-12T13:35:00Z",
                                  "2026-08-12T19:00:00Z"]

    def test_bars_after_the_flatten_are_dropped(self):
        # 20:00Z = 16:00 ET, after the 15:50 flatten: nothing is held.
        rows = {"AAA": [bar("2026-08-12T20:00:00Z", 1, 1, 1, 1)]}
        assert replay.bars_by_minute(rows, CFG) == {}

    def test_entry_cutoff_is_the_narrower_window(self):
        assert replay.in_session("2026-08-12T16:00:00Z", CFG)      # 12:00 ET
        assert not replay.in_session("2026-08-12T19:00:00Z", CFG)  # 15:00 ET

    def test_premarket_inside_the_window_is_kept(self):
        # 12:00Z = 08:00 ET, after the 07:30 start.
        rows = {"AAA": [bar("2026-08-12T12:00:00Z", 1, 1, 1, 1)]}
        assert len(replay.bars_by_minute(rows, CFG)) == 1

    def test_overnight_bars_are_dropped(self):
        # 09:00Z = 05:00 ET, before the session starts.
        rows = {"AAA": [bar("2026-08-12T09:00:00Z", 1, 1, 1, 1)]}
        assert replay.bars_by_minute(rows, CFG) == {}

    def test_no_config_means_no_filtering(self):
        rows = {"AAA": [bar("2026-08-12T19:00:00Z", 1, 1, 1, 1)]}
        assert len(replay.bars_by_minute(rows)) == 1


class TestSweepGates:
    """The sweep decides which rows a threshold set admits."""

    def _features(self, **over):
        base = {"rvol": 8.0, "float_shares": 8e6, "day_pct": 25.0,
                "dist_from_hod": 0.5, "catalyst_score": 0.8, "above_vwap": 1.0}
        base.update(over)
        return base

    def _combo(self, **over):
        base = {"rvol": 5.0, "float_max": 20e6, "pct_up": 10.0,
                "dist_hod": 4.0, "catalyst": 0.3, "vwap": True}
        base.update(over)
        return base

    def test_a_clean_row_passes(self):
        from scripts import sweep
        assert sweep.passes(self._features(), self._combo())

    def test_each_gate_can_reject_on_its_own(self):
        from scripts import sweep
        for field, value in [("rvol", 1.0), ("float_shares", 500e6),
                             ("day_pct", 2.0), ("dist_from_hod", 20.0),
                             ("catalyst_score", 0.0), ("above_vwap", 0.0)]:
            assert not sweep.passes(self._features(**{field: value}),
                                    self._combo()), field

    def test_loosening_a_gate_admits_what_it_rejected(self):
        from scripts import sweep
        thin = self._features(rvol=2.5)
        assert not sweep.passes(thin, self._combo())
        assert sweep.passes(thin, self._combo(rvol=2.0))

    def test_unknown_float_is_never_admitted(self):
        """No float data is not the same as a small float."""
        from scripts import sweep
        assert not sweep.passes(self._features(float_shares=0), self._combo())

    def test_win_rate_is_measured_only_over_admitted_rows(self):
        from scripts import sweep
        rows = [("2026-08-01", self._features(), 1),
                ("2026-08-01", self._features(), 0),
                ("2026-08-01", self._features(rvol=1.0), 1)]   # rejected
        n, rate = sweep.score(rows, self._combo())
        assert (n, rate) == (2, 0.5)


def test_grading_continues_past_the_entry_cutoff_but_recording_stops(tmp_path):
    """The two windows do different jobs.

    A trade opened in the morning has to be graded on the afternoon it
    actually had - otherwise a four-hour hold is scored as a timeout, which
    is the artifact the longer horizon exists to remove. But nothing new may
    be recorded after 12:15 ET, because the live bot could not have entered
    it.
    """
    journal = Journal(str(tmp_path / "backtest.db"),
                      alert_window_minutes=CFG.bot_alert_window_minutes)
    day = "2026-08-12"

    morning = pullback_bars(day, v=40_000)    # 09:30-09:33 ET, triggers
    # 17:00Z = 13:00 ET: past the entry cutoff, inside the 4-hour hold.
    morning.append(bar(f"{day}T17:00:00Z", 6.2, 9.00, 6.1, 8.90, v=40_000))
    # A mover that only shows up in the afternoon must never be recorded.
    late = [bar(f"{day}T17:00:00Z", 5.0, 9.00, 4.9, 8.90, v=40_000)]

    news = [{"symbol": s, "headline": f"{s} receives FDA approval",
             "ts": int(dt.datetime.fromisoformat(
                 f"{day}T13:00:00+00:00").timestamp()),
             "url": "u", "source": "bz"} for s in ("MOVR", "LATE")]
    context = {"prev_close": {"MOVR": 4.00, "LATE": 4.00},
               "avg_volume": {"MOVR": 400_000, "LATE": 400_000},
               "float_shares": {"MOVR": 8_000_000, "LATE": 8_000_000}}

    replay.replay_day(day, {"MOVR": morning, "LATE": late}, news, context,
                      journal, CFG)

    symbols = {a["symbol"] for a in journal.recent_alerts(20)}
    assert "MOVR" in symbols
    assert "LATE" not in symbols          # arrived after the entry cutoff

    movr = [a for a in journal.recent_alerts(20) if a["symbol"] == "MOVR"][0]
    assert movr["label"] == 1             # the 13:00 ET bar graded it a win


class TestBaselineLookback:
    """A month-at-a-time schedule must not starve the volume baseline.

    rvol compares today against a 30-session average, and prev_close needs
    yesterday. Fetching daily bars from the replay's own start date would
    give the first weeks of every month a baseline of one or two days, so
    the lookback is fetched and then excluded from the replay itself.
    """

    def test_lookback_reaches_back_past_thirty_sessions(self):
        from scripts.backtest import _lookback_start
        assert _lookback_start("2026-01-01") == "2025-11-02"

    def test_lookback_days_are_never_replayed(self):
        from scripts.backtest import _lookback_start
        start, end = "2026-02-01", "2026-02-28"
        candidates = {"2026-01-05": ["AAA"], "2026-02-03": ["BBB"],
                      "2026-02-27": ["CCC"], "2026-03-02": ["DDD"]}
        days = [d for d in sorted(candidates) if start <= d <= end]
        assert days == ["2026-02-03", "2026-02-27"]
        assert _lookback_start(start) < "2026-01-05"    # baseline covers it


class TestOutcomeSplit:
    """A stop-out and a timeout are both label 0, and are not the same trade."""

    def _row(self, label, mae, mfe=0.0, r=1.0):
        return {"day": "2026-08-12", "label": label, "mae": mae, "mfe": mfe,
                "r_dollars": r}

    def test_classifies_the_three_outcomes(self):
        from scripts.backtest import outcome
        assert outcome(self._row(1, -0.2, mfe=2.0)) == "win"
        assert outcome(self._row(0, -1.5)) == "stopped"
        assert outcome(self._row(0, -0.3)) == "timeout"

    def test_timeouts_do_not_cost_a_full_r(self):
        from scripts.backtest import _expectancy
        rows = [self._row(0, -0.3) for _ in range(10)]
        assert _expectancy(rows, CFG, timeout_r_value=-1.0)[1] == pytest.approx(-1.0)
        assert _expectancy(rows, CFG, timeout_r_value=0.0)[1] == pytest.approx(0.0)

    def test_a_stop_out_costs_a_full_r_either_way(self):
        from scripts.backtest import _expectancy
        rows = [self._row(0, -1.2) for _ in range(10)]
        assert _expectancy(rows, CFG, timeout_r_value=0.0)[1] == pytest.approx(-1.0)


class TestMeasuredTimeoutExit:
    """The timeout value is recorded, not assumed.

    Timeouts are the large majority of outcomes, so
    assuming they scratch at 0R would have decided the result rather than
    measured it.
    """

    def test_a_timeout_records_where_it_actually_closed(self, tmp_path):
        j = Journal(str(tmp_path / "j.db"), alert_window_minutes=240)
        aid = j.record_alert(1_700_000_000, "HODX", price=5.00,
                             r_dollars=1.00, features={"rvol": 8.0})
        # 4h01m later at 4.70: never hit -1R (4.00) or +2R (7.00).
        j.track_alert(aid, 1_700_000_000 + 14_460, price=4.70,
                      high=4.75, low=4.65)
        row = j.outcome_rows()[0]
        assert row["label"] == 0
        assert row["resolved_r"] == pytest.approx(-0.30)

    def test_a_stop_out_and_a_win_record_their_levels(self, tmp_path):
        j = Journal(str(tmp_path / "j.db"), alert_window_minutes=240)
        loser = j.record_alert(1_700_000_000, "AAA", 5.00, 1.00, {})
        j.track_alert(loser, 1_700_000_060, price=4.10, high=4.5, low=3.90)
        winner = j.record_alert(1_700_000_000, "BBB", 5.00, 1.00, {})
        j.track_alert(winner, 1_700_000_060, price=7.10, high=7.2, low=5.0)
        by_symbol = {r["symbol"]: r for r in j.outcome_rows()}
        assert by_symbol["AAA"]["resolved_r"] == pytest.approx(-1.0)
        assert by_symbol["BBB"]["resolved_r"] == pytest.approx(2.0)

    def test_expectancy_uses_the_measured_value(self):
        from scripts.backtest import _expectancy
        rows = [{"label": 0, "mae": -0.3, "mfe": 0.1, "r_dollars": 1.0,
                 "resolved_r": -0.4} for _ in range(10)]
        assert _expectancy(rows, CFG)[1] == pytest.approx(-0.4)
        assert _expectancy(rows, CFG, timeout_r_value=0.0)[1] == pytest.approx(0.0)


class TestPolicyExpectancy:
    """Alerts are scored under the exit policy the bot actually trades."""

    def _win(self, mfe, r=0.25):
        return {"day": "2026-08-12", "label": 1, "mae": -0.05, "mfe": mfe,
                "r_dollars": r, "resolved_r": None}

    def test_the_target_is_worth_more_on_a_cheap_stock(self):
        from scripts.backtest import target_in_r
        assert target_in_r(self._win(0.2, r=0.25), CFG) == pytest.approx(0.8)
        assert target_in_r(self._win(0.2, r=0.05), CFG) == pytest.approx(4.0)

    def test_a_win_that_goes_nowhere_pays_only_the_banked_share(self):
        from scripts.backtest import alert_r
        # Tagged +20c (0.8R here) and stopped dead: the 35% runner trails out
        # at break-even, so only the banked 65% pays.
        assert alert_r(self._win(0.20), CFG) == pytest.approx(0.65 * 0.8)

    def test_a_runner_that_keeps_going_adds_to_it(self):
        from scripts.backtest import alert_r
        # Ran 3R; the 5% trail against the 5% stop gives back 1R of it.
        expected = 0.65 * 0.8 + 0.35 * (3.0 - 1.0)
        assert alert_r(self._win(0.75), CFG) == pytest.approx(expected)

    def test_the_runner_is_never_worth_less_than_nothing(self):
        from scripts.backtest import alert_r
        assert alert_r(self._win(0.20), CFG) > 0

    def test_a_stop_out_is_still_a_full_r(self):
        from scripts.backtest import alert_r
        stopped = {"day": "d", "label": 0, "mae": -0.30, "mfe": 0.0,
                   "r_dollars": 0.25, "resolved_r": -1.0}
        assert alert_r(stopped, CFG) == pytest.approx(-1.0)

    def test_the_sweep_scores_alerts_the_same_way(self):
        """Two definitions of what an alert was worth would drift apart."""
        import scripts.sweep as sweep
        from scripts.backtest import alert_r
        assert sweep.alert_r is alert_r


class TestHalfHourReport:
    """The pre-market decision is read off this table, so it has to split at
    the right boundaries and compute the margin correctly."""

    @staticmethod
    def _trade(hh, mm, r, reason):
        ts = int(dt.datetime(2026, 3, 10, hh, mm,
                             tzinfo=dt.timezone(dt.timedelta(hours=-4))).timestamp())
        return {"ts": ts, "r_multiple": r, "pnl": r * 50, "exit_reason": reason}

    def test_the_bell_is_a_bucket_boundary(self, capsys):
        from scripts.backtest import _hour_report
        rows = [self._trade(9, 29, 1.0, "trailing"),
                self._trade(9, 31, -1.0, "stop")]
        _hour_report(rows, CFG)
        out = capsys.readouterr().out
        assert "09:00 ET" in out and "09:30 ET" in out

    def test_the_session_split_says_what_premarket_can_absorb(self, capsys):
        """+0.2R a trade with half of them stopped absorbs 0.4R per stop."""
        from scripts.backtest import _session_split
        rows = [self._trade(8, 0, 1.4, "trailing"),
                self._trade(8, 5, -1.0, "stop"),
                self._trade(10, 0, -1.0, "stop")]
        _session_split(rows)
        pre = next(l for l in capsys.readouterr().out.splitlines()
                   if "before 09:30" in l)
        assert "+0.200R" in pre and "50.0%" in pre and "+0.40R" in pre

    def test_a_losing_block_absorbs_nothing(self, capsys):
        from scripts.backtest import _session_split
        _session_split([self._trade(8, 0, -1.0, "stop"),
                        self._trade(8, 5, -0.5, "stall")])
        pre = next(l for l in capsys.readouterr().out.splitlines()
                   if "before 09:30" in l)
        assert pre.rstrip().endswith("-")
