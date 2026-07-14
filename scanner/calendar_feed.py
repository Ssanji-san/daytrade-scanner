"""ForexFactory economic calendar: keep red/orange (High/Medium) events.

The weekly JSON feed's dates carry their own UTC offset; we convert to
epoch seconds and let the browser render them in the user's local time.
"""
import datetime as dt

from .config import Config


def filter_events(events, cfg: Config):
    out = []
    for ev in events:
        if ev.get("impact") not in cfg.calendar_impacts:
            continue
        try:
            ts = dt.datetime.fromisoformat(ev["date"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "title": ev.get("title", ""),
            "country": ev.get("country", ""),
            "impact": ev["impact"],
            "ts": int(ts.timestamp()),
            "forecast": ev.get("forecast", ""),
            "previous": ev.get("previous", ""),
        })
    out.sort(key=lambda e: e["ts"])
    return out
