import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.state import MarketState

ET = ZoneInfo("America/New_York")
CFG = Config()


def t(hour, minute, second=0):
    return dt.datetime(2026, 7, 14, hour, minute, second, tzinfo=ET)


def snap(price, cum_volume=2_000_000, day_high=None, prev_close=2.20,
         avg_volume=400_000, float_shares=8_000_000, bar_open=None,
         bar_t="2026-07-14T14:00:00Z"):
    """One snapshot. The bar is stamped 10:00 ET so it lands after the bell.

    `bar_open` defaults to ~12% below price, which is what freezes a
    positive open_pct: the opening drive these tests assume is happening.
    """
    return {"price": price, "cum_volume": cum_volume,
            "day_high": day_high if day_high is not None else price,
            "prev_close": prev_close, "avg_volume": avg_volume,
            "float_shares": float_shares,
            "minute_bar": {"t": bar_t,
                           "o": bar_open if bar_open is not None
                                else round(price / 1.12, 4),
                           "h": price, "l": price, "c": price, "v": 5000}}


def feed_flat_then_spike(state):
    """FLAT stays at $10; MOVR spikes 10% over the last 5 minutes."""
    start = t(12, 0)
    for i in range(41):  # 20 min, one sample / 30 s
        now = start + dt.timedelta(seconds=30 * i)
        minutes = 30 * i / 60
        movr = 10.0 if minutes <= 15 else 10.0 * (1 + 0.02 * (minutes - 15))
        state.ingest(now, {"FLAT": snap(10.0), "MOVR": snap(round(movr, 4))})
    return start + dt.timedelta(minutes=20)


def test_planted_mover_tops_five_minute_gainers():
    state = MarketState(CFG)
    now = feed_flat_then_spike(state)
    payload = state.payload(now)
    rows = payload["gainers"]["5"]
    assert rows and rows[0]["symbol"] == "MOVR"
    assert rows[0]["changes"]["5"] == pytest.approx(10.0, abs=1.5)
    assert all(r["symbol"] != "FLAT" for r in rows)


def test_planted_low_float_qualifies_for_hod():
    state = MarketState(CFG)
    now = t(12, 45)  # half the session elapsed -> rvol = 2M / 200k = 10
    state.ingest(now, {"HODX": snap(3.00, day_high=3.03)})
    state.set_news(now, [{"symbol": "HODX", "headline": "FDA approval",
                          "ts": int(now.timestamp()), "url": "u", "source": "bz"}])
    payload = state.payload(now)
    assert [r["symbol"] for r in payload["hod"]["qualified"]] == ["HODX"]
    row = payload["hod"]["qualified"][0]
    assert row["day_pct"] == pytest.approx(36.364, abs=0.01)  # 2.20 -> 3.00
    assert row["rvol"] == pytest.approx(10.0)
    assert row["has_news"] is True


def test_news_older_than_max_age_is_not_a_catalyst():
    state = MarketState(CFG)
    now = t(12, 45)
    old = now - dt.timedelta(hours=CFG.news_max_age_hours + 1)
    state.ingest(now, {"HODX": snap(3.00, day_high=3.03)})
    state.set_news(now, [{"symbol": "HODX", "headline": "old news",
                          "ts": int(old.timestamp()), "url": "u", "source": "bz"}])
    row = state.payload(now)["hod"]["qualified"][0]
    assert row["has_news"] is False


def test_stale_banner_after_ingest_gap():
    state = MarketState(CFG)
    now = t(12, 0)
    state.ingest(now, {"FLAT": snap(10.0)})
    assert state.payload(now + dt.timedelta(seconds=2))["stale_since"] is None
    stale = state.payload(now + dt.timedelta(seconds=60))
    assert stale["stale_since"] == int(now.timestamp())
    # last-good data still served
    assert stale["hod"] is not None and "gainers" in stale


def test_remembers_float_and_avg_volume_once_seen():
    state = MarketState(CFG)
    now = t(12, 45)
    state.ingest(now, {"HODX": snap(3.00, day_high=3.03)})
    later = now + dt.timedelta(seconds=30)
    bare = {"price": 3.02, "cum_volume": 2_100_000, "day_high": 3.03,
            "prev_close": 2.20, "avg_volume": None, "float_shares": None}
    state.ingest(later, {"HODX": bare})
    row = state.payload(later)["hod"]["qualified"][0]
    assert row["float_shares"] == 8_000_000
    assert row["rvol"] is not None


def _headline(symbol, ts, headline="h"):
    return {"symbol": symbol, "headline": headline, "ts": int(ts), "url": "u",
            "source": "bz"}


def test_calendar_and_news_feed_in_payload():
    state = MarketState(CFG)
    now = t(12, 0)
    state.ingest(now, {"FLAT": snap(10.0)})
    state.set_calendar([{"title": "CPI", "impact": "High", "ts": 1}])
    state.set_news(now, [_headline("FLAT", now.timestamp() - 600)])
    payload = state.payload(now)
    assert payload["calendar"][0]["title"] == "CPI"
    assert payload["news"][0]["symbol"] == "FLAT"


class TestNewsIsMerged:
    """A catalyst must not vanish because one batch left the symbol out.

    The feed answers with the newest headlines across every candidate at
    once and does not paginate, so a quiet small cap drops out of the batch
    constantly while the megacaps never do. Replacing the store wholesale
    made the stock look newsless until it happened to come back - and news
    is the gate the whole strategy hangs on.
    """

    def test_a_symbol_missing_from_the_next_batch_keeps_its_catalyst(self):
        state = MarketState(CFG)
        now = t(12, 0)
        state.set_news(now, [_headline("SMALL", now.timestamp() - 600,
                                       "SMALL wins FDA approval")])
        later = now + dt.timedelta(minutes=1)
        state.set_news(later, [_headline("BIGCO", later.timestamp() - 30)])

        assert state._has_news("SMALL", later)
        assert {n["symbol"] for n in state.news} == {"SMALL", "BIGCO"}

    def test_a_headline_is_not_recorded_twice(self):
        state = MarketState(CFG)
        now = t(12, 0)
        item = _headline("SMALL", now.timestamp() - 600)
        state.set_news(now, [item])
        state.set_news(now + dt.timedelta(seconds=30), [dict(item)])
        assert len(state.news) == 1

    def test_a_headline_older_than_the_window_is_evicted(self):
        state = MarketState(CFG)
        now = t(12, 0)
        state.set_news(now, [_headline("OLD", now.timestamp() - 600)])
        much_later = now + dt.timedelta(hours=CFG.news_max_age_hours + 1)
        state.set_news(much_later, [])
        assert state.news == []
        assert not state._has_news("OLD", much_later)


def _gapper_snap(price, when, prev_close=5.0):
    return {"price": price, "cum_volume": 500_000, "day_high": price,
            "prev_close": prev_close, "avg_volume": 400_000,
            "float_shares": 8_000_000,
            "minute_bar": {"t": when.astimezone(dt.timezone.utc).isoformat(),
                           "o": price, "h": price, "l": round(price * 0.99, 4),
                           "c": price, "v": 20_000}}


def test_freezes_the_gap_and_the_opening_range_at_the_bell():
    """Both are captured live because they cannot be recovered later.

    SymbolHistory keeps only the last 180 one-minute bars, so on a session
    that starts premarket the opening bars roll off well before noon.
    """
    state = MarketState(CFG)

    premarket = t(8, 0)                       # gapped to 15.00 from a 5.00 close
    state.ingest(premarket, {"GAPR": _gapper_snap(15.00, premarket)})
    assert state._gap_pct["GAPR"] == pytest.approx(200.0)

    for i in range(5):                        # 9:30-9:34 carve the range
        when = t(9, 30 + i)
        state.ingest(when, {"GAPR": _gapper_snap(15.00 + i * 0.10, when)})
    assert "GAPR" not in state._opening_range  # still forming

    after = t(9, 36)
    state.ingest(after, {"GAPR": _gapper_snap(15.60, after)})
    opening = state._opening_range["GAPR"]
    assert opening["high"] == pytest.approx(15.40)
    assert opening["low"] == pytest.approx(14.85)
    assert state._gap_pct["GAPR"] == pytest.approx(200.0)   # still the gap


def test_a_gapper_breaking_its_opening_range_becomes_a_setup():
    """No flag has formed yet, so the range break is what fires.

    Price runs straight up out of the range, so the swing high is the newest
    bar and detect_pullback has nothing to work with - which is exactly the
    gap-and-go case the opening range exists to cover.
    """
    state = MarketState(CFG)
    premarket = t(8, 0)
    state.ingest(premarket, {"GAPR": _gapper_snap(15.00, premarket)})
    for i in range(5):                         # 9:30-9:34, range high 15.40
        when = t(9, 30 + i)
        state.ingest(when, {"GAPR": _gapper_snap(15.00 + i * 0.10, when)})

    breaking = t(9, 36)
    state.ingest(breaking, {"GAPR": _gapper_snap(15.60, breaking)})
    row = {s["symbol"]: s for s in state.build_states(breaking)}["GAPR"]
    assert row["setup"]["setup"] == "opening_range"
    assert row["setup"]["stop"] == pytest.approx(14.85)   # the range low
    assert row["gap_pct"] == pytest.approx(200.0)


def test_a_flag_takes_precedence_over_the_opening_range():
    """When both are available the pullback wins - it has the tighter stop."""
    state = MarketState(CFG)
    premarket = t(8, 0)
    state.ingest(premarket, {"GAPR": _gapper_snap(15.00, premarket)})
    for i in range(5):
        when = t(9, 30 + i)
        state.ingest(when, {"GAPR": _gapper_snap(15.00 + i * 0.10, when)})
    dip = t(9, 36)                             # shallow dip, then a new high
    state.ingest(dip, {"GAPR": _gapper_snap(15.35, dip)})
    breaking = t(9, 37)
    state.ingest(breaking, {"GAPR": _gapper_snap(15.60, breaking)})

    row = {s["symbol"]: s for s in state.build_states(breaking)}["GAPR"]
    assert row["setup"]["setup"] == "micro_pullback"
    assert row["setup"]["stop"] > 14.85        # tighter than the range low


class TestOpeningDrive:
    """open_pct measures the move since the 9:30 bell, not since yesterday.

    A stock that gapped 40% overnight and has drifted sideways since is a
    different trade from one grinding up off the open, and day_pct cannot
    tell them apart.
    """

    def test_freezes_the_price_at_the_bell(self):
        state = MarketState(CFG)
        now = t(9, 45)
        state.ingest(now, {"HODX": snap(3.00, bar_open=2.70,
                                        bar_t="2026-07-14T13:31:00Z")})
        row = [s for s in state.build_states(now) if s["symbol"] == "HODX"][0]
        assert row["open_pct"] == pytest.approx(11.111, abs=0.01)

        # Later bars must not move it - it is the OPEN, not the last print.
        later = t(9, 50)
        state.ingest(later, {"HODX": snap(3.60, bar_open=3.55,
                                          bar_t="2026-07-14T13:36:00Z")})
        row = [s for s in state.build_states(later) if s["symbol"] == "HODX"][0]
        assert row["open_pct"] == pytest.approx(33.333, abs=0.01)  # 3.60 vs 2.70

    def test_premarket_bars_do_not_set_the_open(self):
        state = MarketState(CFG)
        now = t(9, 0)
        state.ingest(now, {"HODX": snap(3.00, bar_open=2.70,
                                        bar_t="2026-07-14T13:00:00Z")})
        row = [s for s in state.build_states(now) if s["symbol"] == "HODX"][0]
        assert row["open_pct"] is None

    def test_a_gapper_that_drifts_sideways_is_rejected(self):
        # Gapped from 2.20 to 3.00 (+36% day_pct) but has gone nowhere
        # since the bell, so the opening-drive gate turns it away.
        state = MarketState(CFG)
        now = t(9, 45)
        state.ingest(now, {"DRIFT": snap(3.00, bar_open=2.99,
                                         bar_t="2026-07-14T13:31:00Z")})
        payload = state.payload(now)
        assert payload["hod"]["qualified"] == []
        near = payload["hod"]["near"][0]
        assert "open_drive" in near["failed"]
        assert near["day_pct"] > 30          # it looked great on day_pct


def test_payload_states_the_gates_actually_in_force():
    """The panel header used to be hardcoded and drifted two config changes
    out of date - advertising $1-$20 while the bot traded $1-$5, which
    reads as a broken scanner rather than a stale label."""
    state = MarketState(CFG)
    now = t(9, 45)
    state.ingest(now, {"HODX": snap(3.00)})
    c = state.payload(now)["criteria"]
    assert c["min_price"] == CFG.hod_min_price
    assert c["max_price"] == CFG.hod_max_price
    assert c["observe_max_price"] == CFG.hod_observe_max_price
    assert c["max_float"] == CFG.hod_max_float
    assert c["min_rvol"] == CFG.hod_min_rvol
    assert c["min_open_pct"] == CFG.hod_min_open_pct
