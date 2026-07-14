"""The synthetic demo session must light up every dashboard panel correctly."""
import datetime as dt

from scanner.calendar_feed import filter_events
from scanner.config import Config
from scanner.demo import build_demo_session
from scanner.state import MarketState

CFG = Config()


def play_session(session):
    state = MarketState(CFG)
    for frame in session["frames"]:
        now = dt.datetime.fromtimestamp(frame["ts"], dt.timezone.utc)
        state.ingest(now, frame["symbols"])
    state.set_news(now, session["news"])
    state.set_calendar(filter_events(session["calendar_events"], CFG))
    return state.payload(now)


def test_planted_mover_tops_gainers():
    payload = play_session(build_demo_session(CFG))
    assert payload["gainers"]["5"][0]["symbol"] == "MOVR"


def test_planted_low_float_hod_qualifies_with_news_badge():
    payload = play_session(build_demo_session(CFG))
    qualified = {r["symbol"]: r for r in payload["hod"]["qualified"]}
    assert "HODX" in qualified
    assert qualified["HODX"]["has_news"] is True
    assert qualified["HODX"]["failed"] == []


def test_near_miss_lands_in_near_list_with_reason():
    payload = play_session(build_demo_session(CFG))
    near = {r["symbol"]: r for r in payload["hod"]["near"]}
    assert "NEARX" in near
    assert len(near["NEARX"]["failed"]) == 1


def test_calendar_only_red_and_orange():
    payload = play_session(build_demo_session(CFG))
    impacts = {e["impact"] for e in payload["calendar"]}
    assert impacts and impacts <= {"High", "Medium"}


def test_not_stale_and_news_present():
    payload = play_session(build_demo_session(CFG))
    assert payload["stale_since"] is None
    assert any(n["symbol"] == "HODX" for n in payload["news"])
