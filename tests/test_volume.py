"""Real (consolidated) volume on the free plan.

IEX sees a few percent of the tape, so "500K shares" has to be counted on
SIP. SIP is free once 15 minutes old: real volume is SIP up to the cutoff
plus IEX since - a floor, never an overstatement."""
import datetime as dt

from scanner.volume import SIP_DELAY, SipTape, real_volume, sip_cutoff


def bar(t, v):
    return {"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": v}


SIP = [bar("2026-08-12T13:30:00Z", 300_000),
       bar("2026-08-12T13:31:00Z", 200_000),
       bar("2026-08-12T13:50:00Z", 900_000)]      # not free yet at 14:00
IEX = [bar("2026-08-12T13:44:00Z", 5_000),         # before the cutoff
       bar("2026-08-12T13:45:00Z", 7_000),
       bar("2026-08-12T13:50:00Z", 30_000)]

UNTIL = dt.datetime(2026, 8, 12, 13, 45, tzinfo=dt.timezone.utc)


def tape(bars, until=UNTIL):
    t = SipTape()
    t.add(bars)
    t.until = until
    return t


def test_sip_before_the_cutoff_plus_iex_after():
    assert real_volume(tape(SIP), IEX) == 300_000 + 200_000 + 7_000 + 30_000


def test_no_sip_reading_is_unknown():
    assert real_volume(None, IEX) is None
    assert real_volume(tape(SIP, until=None), IEX) is None


def test_an_empty_sip_answer_is_zero_not_unknown():
    """SIP answered and nothing traded: only IEX since the cutoff counts."""
    assert real_volume(tape([]), IEX) == 37_000


def test_the_same_bar_twice_counts_once():
    """The live loop refetches overlapping windows; a bar is a bar."""
    t = tape(SIP)
    t.add(SIP[:2])
    assert real_volume(t, []) == 500_000


def test_moving_the_cutoff_releases_more_sip():
    t = tape(SIP, until=dt.datetime(2026, 8, 12, 13, 31,
                                    tzinfo=dt.timezone.utc))
    assert real_volume(t, []) == 300_000
    t.until = dt.datetime(2026, 8, 12, 14, 0, tzinfo=dt.timezone.utc)
    assert real_volume(t, []) == 1_400_000


def test_cutoff_is_sixteen_minutes_back():
    now = dt.datetime(2026, 8, 12, 14, 1, 30, tzinfo=dt.timezone.utc)
    assert SIP_DELAY >= dt.timedelta(minutes=15)
    assert sip_cutoff(now) == dt.datetime(2026, 8, 12, 13, 45,
                                          tzinfo=dt.timezone.utc)
