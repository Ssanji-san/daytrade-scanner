"""In-memory rolling history per symbol + relative-volume math.

The poll loop appends one (timestamp, price, cumulative volume) sample per
cycle; scanners read N-minute changes from it. Pure computation, no I/O.
"""
import bisect
import datetime as dt
from collections import deque
from zoneinfo import ZoneInfo

from .config import Config

ET = ZoneInfo("America/New_York")
SESSION_OPEN = dt.time(9, 30)
SESSION_MINUTES = 390  # 9:30 -> 16:00


class SymbolHistory:
    def __init__(self, maxlen=2400):
        self._samples = deque(maxlen=maxlen)  # (ts, price, cum_volume), ts ascending

    def add(self, ts, price, cum_volume):
        self._samples.append((ts, price, cum_volume))

    def __len__(self):
        return len(self._samples)

    @property
    def latest(self):
        return self._samples[-1] if self._samples else None

    def n_minute_change(self, now, minutes):
        """% price change vs the last sample at or before `now - minutes`."""
        if not self._samples:
            return None
        target = now - dt.timedelta(minutes=minutes)
        times = [s[0] for s in self._samples]
        i = bisect.bisect_right(times, target) - 1
        if i < 0:
            return None
        base = self._samples[i][1]
        current = self._samples[-1][1]
        if not base:
            return None
        return 100.0 * (current - base) / base


def session_fraction(now, cfg: Config):
    """Fraction of the 9:30-16:00 ET session elapsed, floored/capped."""
    et = now.astimezone(ET)
    open_dt = et.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute,
                         second=0, microsecond=0)
    elapsed = (et - open_dt).total_seconds() / 60.0
    fraction = elapsed / SESSION_MINUTES
    return min(1.0, max(cfg.rvol_min_session_fraction, fraction))


def rvol(cum_volume_today, avg_daily_volume, now, cfg: Config):
    """Relative volume: today's pace vs the average day's pace so far."""
    if not avg_daily_volume:
        return None
    expected = avg_daily_volume * session_fraction(now, cfg)
    return cum_volume_today / expected
