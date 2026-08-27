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
from ..history import ET
from ..state import MarketState
from ..trading.bot import journal_alert
from ..trading.journal import Journal


def in_session(stamp, cfg: Config):
    """Is this bar inside the hours a live session actually watches?"""
    try:
        et = dt.datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00")).astimezone(ET)
    except ValueError:
        return False
    open_h, open_m = (int(p) for p in cfg.backtest_open_et.split(":"))
    close_h, close_m = (int(p) for p in cfg.backtest_close_et.split(":"))
    return (open_h, open_m) <= (et.hour, et.minute) <= (close_h, close_m)


def bars_by_minute(minute_bars, cfg: Config = None):
    """{minute: {symbol: bar}}, oldest first, inside the session window.

    `minute_bars` is Alpaca's {symbol: [bar, ...]} for one session. Bars
    outside the hours the bot runs are dropped: a setup at 15:00 is not one
    it could ever have taken.
    """
    timeline = {}
    for symbol, rows in (minute_bars or {}).items():
        for bar in rows:
            if not bar.get("t") or not bar.get("c"):
                continue
            if cfg is not None and not in_session(bar["t"], cfg):
                continue
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
    timeline = bars_by_minute(minute_bars, cfg)
    last_bar = {}
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

        last_bar.update(symbol_bars)

        # Grade what is already open against this bar's real high and low,
        # so a wick through the stop counts as the loss it was. Scoped to
        # this session: yesterday's leftovers must not be marked with
        # today's prices.
        for alert_id, symbol in journal.open_alerts(day=day):
            bar = symbol_bars.get(symbol)
            if bar:
                journal.track_alert(alert_id, now_ts, bar["c"],
                                    high=bar.get("h"), low=bar.get("l"))

    # One last mark at the closing print. A thin symbol can stop printing
    # long before the bell, leaving an alert that never got the update that
    # would time it out - and an unlabeled alert teaches the model nothing.
    # Anything still inside its 30-minute window stays open, because that is
    # missing data rather than a loss.
    if timeline:
        final = dt.datetime.fromisoformat(
            list(timeline)[-1].replace("Z", "+00:00"))
        final_ts = int(final.timestamp())
        for alert_id, symbol in journal.open_alerts(day=day):
            bar = last_bar.get(symbol)
            if bar:
                journal.track_alert(alert_id, final_ts, bar["c"],
                                    high=bar.get("h"), low=bar.get("l"))
    return seen
