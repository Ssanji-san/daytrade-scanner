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
from dataclasses import replace

from ..config import Config
from ..history import ET
from ..state import MarketState
from ..trading.bot import journal_alert
from ..trading.journal import Journal


def _hhmm(text):
    hour, minute = text.split(":")
    return int(hour), int(minute)


def in_session(stamp, cfg: Config, close_et=None):
    """Is this bar inside the hours a live session actually watches?

    `close_et` overrides the entry cutoff. Two different windows matter: new
    alerts are only recorded while the live bot could still enter, but bars
    keep flowing afterwards so an open position can be graded to its end.
    """
    try:
        et = dt.datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00")).astimezone(ET)
    except ValueError:
        return False
    close = close_et or cfg.backtest_close_et
    return _hhmm(cfg.backtest_open_et) <= (et.hour, et.minute) <= _hhmm(close)


def bars_by_minute(minute_bars, cfg: Config = None):
    """{minute: {symbol: bar}}, oldest first, inside the tracking window.

    `minute_bars` is Alpaca's {symbol: [bar, ...]} for one session. Bars run
    to `bot_flatten_time` rather than the entry cutoff - derived from it so
    the replay and the live exit cannot drift apart. A four-hour hold needs
    four hours of bars; cutting them at the entry cutoff would grade every
    late trade as a timeout, which is the artifact this exists to avoid.
    """
    timeline = {}
    for symbol, rows in (minute_bars or {}).items():
        for bar in rows:
            if not bar.get("t") or not bar.get("c"):
                continue
            if cfg is not None and not in_session(bar["t"], cfg,
                                                  cfg.bot_flatten_time):
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


def _mark(journal, tracked, ts, symbol_bars):
    """Grade open alerts against this bar's real high and low.

    A wick through the stop counts as the loss it was. Tracked in memory:
    querying every unresolved alert each minute does not scale once the whole
    market is being sampled.
    """
    for symbol, bar in symbol_bars.items():
        alert_id = tracked.get(symbol)
        if alert_id:
            journal.track_alert(alert_id, ts, bar["c"],
                                high=bar.get("h"), low=bar.get("l"))


def _record(journal, tracked, ts, row, now, observed, cfg):
    """Journal a row once per session and remember its id for grading."""
    symbol = row["symbol"]
    if symbol in tracked:
        return None
    alert_id = journal_alert(journal, ts, row, now, observed, cfg)
    tracked[symbol] = alert_id      # None for a duplicate day+symbol
    return alert_id


def replay_day(day, minute_bars, news_items, context, journal: Journal,
               cfg: Config):
    """Replay one session, journalling graded alerts. Returns how many.

    `context` supplies the per-symbol facts a live session would already
    know: {"prev_close": {}, "avg_volume": {}, "float_shares": {}}. Those
    must be computed from data strictly before `day` - see
    `fetch.prior_avg_volume`.
    """
    # Capture wider than the live near-list so a sweep has something to
    # explore; the live gate itself is untouched.
    state = MarketState(replace(
        cfg, near_filter_max_failures=cfg.backtest_near_failures))
    cursor = SessionCursor()
    timeline = bars_by_minute(minute_bars, cfg)
    last_bar = {}
    tracked = {}          # symbol -> alert id, this session only
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

        # Past the entry cutoff the replay still steps bars - open alerts
        # need grading - but it records no new ones, because the live bot
        # could not have entered them.
        if not in_session(minute, cfg):
            last_bar.update(symbol_bars)
            _mark(journal, tracked, now_ts, symbol_bars)
            continue

        payload = state.payload(now, require_news=True)
        for row in payload["hod"]["qualified"]:
            _record(journal, tracked, now_ts, row, now, 0, cfg)
            seen += 1
        if cfg.learn_from_near_misses:
            for row in payload["hod"].get("near") or []:
                _record(journal, tracked, now_ts, row, now, 1, cfg)
                seen += 1
        if cfg.backtest_sample_all:
            # Every mover, gates or not. A model shown only what the filters
            # already surfaced can rank within that set but never learns
            # what a 2R move looks like in the population it is not seeing.
            for row in state.build_states(now):
                if row["symbol"] not in tracked:
                    _record(journal, tracked, now_ts, row, now, 2, cfg)
                    seen += 1

        last_bar.update(symbol_bars)
        _mark(journal, tracked, now_ts, symbol_bars)

    # One last mark at the closing print. A thin symbol can stop printing
    # long before the bell, leaving an alert that never got the update that
    # would time it out - and an unlabeled alert teaches the model nothing.
    # Anything still inside its 30-minute window stays open, because that is
    # missing data rather than a loss.
    if timeline:
        final = dt.datetime.fromisoformat(
            list(timeline)[-1].replace("Z", "+00:00"))
        final_ts = int(final.timestamp())
        for symbol, alert_id in tracked.items():
            bar = last_bar.get(symbol)
            if alert_id and bar:
                journal.track_alert(alert_id, final_ts, bar["c"],
                                    high=bar.get("h"), low=bar.get("l"))
    return seen
