"""Step a historical session through the live pipeline, minute by minute.

The replay never calls the scanners directly. It builds the same
snapshot-shaped dicts the live poll loop builds and pushes them through
`MarketState.ingest`, so VWAP, the opening range, setups, catalyst scoring
and the HOD gate all run exactly as they do in production. If the replay and
the live loop ever disagree, the model would be training on one distribution
and trading in another.

Point-in-time is enforced in one place, `visible_news`, plus the fact that
minutes are replayed in order and cumulative volume/day-high are accumulated
as they happen rather than read off the finished session.
"""
import datetime as dt

from ..config import Config
from ..state import MarketState
from ..trading.bot import journal_alert
from ..trading.journal import Journal


def bars_by_minute(minute_bars):
    """{minute: {symbol: bar}}, oldest first.

    `minute_bars` is Alpaca's {symbol: [bar, ...]} for one session.
    """
    timeline = {}
    for symbol, rows in (minute_bars or {}).items():
        for bar in rows:
            if bar.get("t") and bar.get("c"):
                timeline.setdefault(bar["t"], {})[symbol] = bar
    return dict(sorted(timeline.items()))


def visible_news(items, now_ts):
    """Only headlines already published. The whole backtest rests on this."""
    return [i for i in (items or [])
            if i.get("ts") is not None and i["ts"] <= now_ts]


class SessionCursor:
    """Accumulates the running totals a live snapshot would have carried.

    Cumulative volume and high-of-day are properties of "the session so far",
    so they have to be built up minute by minute. Reading them off the
    completed session would hand the scanner tomorrow's information.
    """

    def __init__(self):
        self.cum_volume = {}
        self.day_high = {}

    def snapshot(self, symbol, bar, prev_close, avg_volume, float_shares):
        self.cum_volume[symbol] = self.cum_volume.get(symbol, 0) + (bar.get("v") or 0)
        self.day_high[symbol] = max(self.day_high.get(symbol, 0), bar.get("h") or 0)
        return {
            "price": bar["c"],
            "cum_volume": self.cum_volume[symbol],
            "day_high": self.day_high[symbol],
            "prev_close": prev_close,
            "avg_volume": avg_volume,
            "float_shares": float_shares,
            "minute_bar": {"t": bar["t"], "o": bar.get("o"), "h": bar.get("h"),
                           "l": bar.get("l"), "c": bar.get("c"),
                           "v": bar.get("v") or 0},
        }


def replay_day(day, minute_bars, news_items, context, journal: Journal,
               cfg: Config):
    """Replay one session, journalling graded alerts. Returns how many.

    `context` supplies the per-symbol facts a live session would already
    know: {"prev_close": {}, "avg_volume": {}, "float_shares": {}}. Those
    must be computed from data strictly before `day` - see
    `fetch.prior_avg_volume`.
    """
    state = MarketState(cfg)
    cursor = SessionCursor()
    timeline = bars_by_minute(minute_bars)
    seen = 0

    for minute, symbol_bars in timeline.items():
        now = dt.datetime.fromisoformat(minute.replace("Z", "+00:00"))
        now_ts = int(now.timestamp())

        symbol_data = {}
        for symbol, bar in symbol_bars.items():
            prev_close = context.get("prev_close", {}).get(symbol)
            if not prev_close:
                continue            # no baseline, every percentage is noise
            symbol_data[symbol] = cursor.snapshot(
                symbol, bar, prev_close,
                context.get("avg_volume", {}).get(symbol),
                context.get("float_shares", {}).get(symbol))
        if not symbol_data:
            continue

        state.ingest(now, symbol_data)
        state.set_news(now, visible_news(news_items, now_ts))

        payload = state.payload(now, require_news=True)
        for row in payload["hod"]["qualified"]:
            journal_alert(journal, now_ts, row, now, 0, cfg)
            seen += 1
        if cfg.learn_from_near_misses:
            for row in payload["hod"].get("near") or []:
                journal_alert(journal, now_ts, row, now, 1, cfg)
                seen += 1

        # Grade what is already open against this bar's real high and low,
        # so a wick through the stop counts as the loss it was.
        for alert_id, symbol in journal.open_alerts():
            bar = symbol_bars.get(symbol)
            if bar:
                journal.track_alert(alert_id, now_ts, bar["c"],
                                    high=bar.get("h"), low=bar.get("l"))
    return seen
