from scanner.calendar_feed import filter_events
from scanner.config import Config

CFG = Config()


def event(title, impact, date="2026-07-14T08:30:00-04:00", country="USD"):
    return {"title": title, "impact": impact, "date": date, "country": country,
            "forecast": "1.2%", "previous": "1.1%"}


def test_keeps_only_red_and_orange():
    events = [event("CPI y/y", "High"), event("Retail Sales", "Medium"),
              event("Nothing burger", "Low"), event("Bank Holiday", "Holiday")]
    out = filter_events(events, CFG)
    assert [e["title"] for e in out] == ["CPI y/y", "Retail Sales"]


def test_parses_date_to_epoch_seconds():
    out = filter_events([event("CPI y/y", "High")], CFG)
    # 2026-07-14 08:30 ET == 12:30 UTC
    assert out[0]["ts"] == 1784032200
    assert out[0]["impact"] == "High"
    assert out[0]["country"] == "USD"
    assert out[0]["forecast"] == "1.2%"


def test_sorted_by_time():
    events = [event("Later", "High", "2026-07-14T14:00:00-04:00"),
              event("Earlier", "Medium", "2026-07-14T08:30:00-04:00")]
    out = filter_events(events, CFG)
    assert [e["title"] for e in out] == ["Earlier", "Later"]


def test_bad_or_missing_date_skipped():
    events = [event("Good", "High"), event("Bad", "High", "not-a-date"),
              {"title": "No date", "impact": "High"}]
    out = filter_events(events, CFG)
    assert [e["title"] for e in out] == ["Good"]
