"""The 14:00 close-out: the session ends at 13:15, so the protect job waits
for the flatten time and closes whatever is still open."""
import datetime as dt

from scanner.config import DEFAULT
from scanner.flatten import seconds_until

ET = __import__("zoneinfo").ZoneInfo("America/New_York")


def et(h, m, s=0):
    return dt.datetime(2026, 10, 13, h, m, s, tzinfo=ET)


def test_waits_until_the_flatten_time():
    assert seconds_until(et(13, 15), "14:00") == 45 * 60
    assert seconds_until(et(13, 59, 30), "14:00") == 30


def test_no_wait_once_it_has_passed():
    assert seconds_until(et(14, 0), "14:00") == 0
    assert seconds_until(et(15, 10), "14:00") == 0


def test_any_timezone():
    assert seconds_until(et(13, 15).astimezone(dt.timezone.utc),
                         "14:00") == 45 * 60


def test_everything_is_out_by_14_00():
    assert DEFAULT.bot_flatten_time == "14:00"
