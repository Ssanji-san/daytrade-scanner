"""Real (consolidated) volume from free data.

The live feed is IEX, a few percent of the tape, so a share-count floor
such as "500K today" means nothing on it. The consolidated SIP tape is free
on Alpaca once it is 15 minutes old. Real volume is therefore SIP volume
up to that cutoff plus IEX volume since: IEX undercounts, so the total is a
floor on the true figure and never overstates it.

The live loop feeds each symbol's SipTape the bars it fetches up to
`sip_cutoff(now)`; the replay loads the whole day's SIP bars once and moves
the cutoff forward a minute at a time.
"""
import bisect
import datetime as dt

# A minute bar is complete a minute after its stamp, and free 15 after that.
SIP_DELAY = dt.timedelta(minutes=16)


def _stamp(bar):
    try:
        return dt.datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def sip_cutoff(now):
    """The latest minute boundary whose SIP bars are free to read."""
    return (now - SIP_DELAY).replace(second=0, microsecond=0)


class SipTape:
    """One symbol's SIP minute volumes, and how far they may be read.

    `until` is the cutoff: bars stamped before it count. Prefix sums keep a
    replay that asks every minute from scanning the whole day each time.
    """

    def __init__(self):
        self._volume = {}          # stamp -> shares; a refetched bar replaces
        self._stamps = None
        self._cum = None
        self.until = None

    def add(self, bars):
        for bar in bars or ():
            stamp = _stamp(bar)
            if stamp is not None:
                self._volume[stamp] = bar.get("v") or 0
        self._stamps = None

    def before(self, until):
        if self._stamps is None:
            self._stamps = sorted(self._volume)
            self._cum = [0]
            for stamp in self._stamps:
                self._cum.append(self._cum[-1] + self._volume[stamp])
        return self._cum[bisect.bisect_left(self._stamps, until)]


def real_volume(tape, iex_bars):
    """SIP shares before the tape's cutoff plus IEX shares from it on.

    None when there is no SIP reading at all - an unknown, which the scanner
    treats as a failure, like an unknown float. An empty SIP answer is a
    reading: nothing traded yet.
    """
    if tape is None or tape.until is None:
        return None
    total = tape.before(tape.until)
    for bar in reversed(list(iex_bars or ())):
        stamp = _stamp(bar)
        if stamp is None:
            continue
        if stamp < tape.until:
            break                  # bars are oldest first; the rest is SIP's
        total += bar.get("v") or 0
    return total
