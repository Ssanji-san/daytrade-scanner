"""Holds live market state and assembles the JSON the dashboard polls.

Both the live poll loop and replay mode push per-symbol dicts through
`ingest()`; everything downstream (history, gainers, HOD scan, news
badges) is computed here so the two modes exercise identical logic.
"""
import datetime as dt
from dataclasses import replace

from . import catalyst, hod, setups
from .config import Config
from .gainers import top_gainers
from .history import ET, SymbolHistory, rvol

MARKET_OPEN = dt.time(9, 30)


def _bar_et(ts):
    """Alpaca bar timestamps are ISO strings; None when unparseable."""
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).astimezone(ET)
    except ValueError:
        return None


class MarketState:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.histories = {}      # symbol -> SymbolHistory
        self.latest = {}         # symbol -> last merged snapshot dict
        self.news = []
        self._news_ts = {}       # symbol -> newest headline epoch
        self._news_by_symbol = {}   # symbol -> [items], for catalyst scoring
        self.calendar = []
        self.last_ingest = None
        # Frozen at the open and left alone: deriving these later from
        # SymbolHistory._bars would fail on a long session, because that
        # deque is maxlen=180 and the opening bars roll off it.
        self._gap_pct = {}       # symbol -> % gap vs prev close at 9:30
        self._opening_range = {}    # symbol -> {"high", "low"} of first N min
        self._open_price = {}    # symbol -> price at the 9:30 bell

    def ingest(self, now, symbol_data):
        """symbol_data: {sym: {price, cum_volume, day_high, prev_close,
        avg_volume, float_shares}}; avg_volume/float_shares stick once seen."""
        self.last_ingest = now
        for sym, data in symbol_data.items():
            history = self.histories.setdefault(sym, SymbolHistory())
            history.add(now, data["price"], data["cum_volume"])
            history.add_bar(data.get("minute_bar"))
            prev = self.latest.get(sym, {})
            merged = dict(data)
            for sticky in ("avg_volume", "float_shares"):
                if merged.get(sticky) is None:
                    merged[sticky] = prev.get(sticky)
            if prev.get("day_high"):
                merged["day_high"] = max(prev["day_high"], merged.get("day_high") or 0)
            self.latest[sym] = merged
            self._mark_open(now, sym, merged, history)

    def _mark_open(self, now, sym, data, history):
        """Freeze the gap and the opening range as they happen."""
        price, prev_close = data.get("price"), data.get("prev_close")
        if not price or not prev_close:
            return
        et = now.astimezone(ET)
        bell = et.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                          second=0, microsecond=0)

        # Premarket keeps re-marking the gap; after the bell the last value
        # stands. A session that starts late still gets one reading.
        if et < bell or sym not in self._gap_pct:
            self._gap_pct[sym] = 100.0 * (price - prev_close) / prev_close

        # The 9:30 print, frozen. day_pct measures the move from
        # yesterday's close, which a gapper has already made overnight -
        # this measures what the stock has done since the bell, which is a
        # different question and the one the opening drive asks.
        if sym not in self._open_price and et >= bell:
            bar = data.get("minute_bar") or {}
            bar_et = _bar_et(bar.get("t"))
            if bar.get("o") and bar_et is not None and bar_et >= bell:
                self._open_price[sym] = bar["o"]

        if sym in self._opening_range or et < bell:
            return
        range_end = bell + dt.timedelta(minutes=self.cfg.orb_minutes)
        if et < range_end:
            return                       # range still forming
        opening = [b for b in history.all_bars
                   if b.get("h") and b.get("l")
                   and (_bar_et(b.get("t")) or bell) >= bell
                   and (_bar_et(b.get("t")) or range_end) < range_end]
        if opening:
            self._opening_range[sym] = {
                "high": max(b["h"] for b in opening),
                "low": min(b["l"] for b in opening),
            }

    def set_news(self, now, items):
        """Merge a batch of headlines into what is already known.

        Replacing wholesale meant a symbol missing from one batch lost its
        catalyst until it happened to come back - and the feed answers with
        the newest headlines across every candidate at once, so a quiet small
        cap drops out of the batch constantly while the megacaps do not. Merge
        instead, and let age do the evicting.
        """
        cutoff = now.timestamp() - self.cfg.news_max_age_hours * 3600
        merged = {sym: list(seen)
                  for sym, seen in self._news_by_symbol.items()}
        for item in items:
            seen = merged.setdefault(item["symbol"], [])
            key = (item.get("ts"), item.get("headline"), item.get("url"))
            if not any((i.get("ts"), i.get("headline"), i.get("url")) == key
                       for i in seen):
                seen.append(item)

        self._news_by_symbol, self._news_ts = {}, {}
        for sym, seen in merged.items():
            fresh = [i for i in seen if (i.get("ts") or 0) >= cutoff]
            if not fresh:
                continue
            self._news_by_symbol[sym] = fresh
            self._news_ts[sym] = max(i["ts"] for i in fresh)
        self.news = sorted(
            (i for seen in self._news_by_symbol.values() for i in seen),
            key=lambda i: -i["ts"])[:100]

    def set_calendar(self, events):
        self.calendar = events

    def _has_news(self, symbol, now):
        newest = self._news_ts.get(symbol)
        if not newest:
            return False
        age_hours = (now.timestamp() - newest) / 3600
        return age_hours <= self.cfg.news_max_age_hours

    def build_states(self, now):
        states = []
        for sym, data in self.latest.items():
            history = self.histories[sym]
            price, prev_close = data["price"], data.get("prev_close")
            day_pct = (100.0 * (price - prev_close) / prev_close
                       if prev_close else None)
            bars = history.all_bars
            symbol_vwap = setups.vwap(bars)
            gap_pct = self._gap_pct.get(sym)
            opening_range = self._opening_range.get(sym)
            open_price = self._open_price.get(sym)
            open_pct = (100.0 * (price - open_price) / open_price
                        if open_price else None)
            # Pullback first; a gapper at the open has no flag to trade yet,
            # so the opening-range break covers exactly that slot.
            setup = setups.detect_pullback(history.completed_bars, price,
                                           self.cfg)
            if setup is None:
                setup = setups.detect_opening_range_break(
                    opening_range, price, gap_pct, self.cfg)
            states.append({
                "catalyst": catalyst.score_news(
                    self._news_by_symbol.get(sym), now.timestamp(), self.cfg),
                "vwap": symbol_vwap,
                "above_vwap": (symbol_vwap is not None
                               and price >= symbol_vwap),
                "gap_pct": gap_pct,
                "open_pct": open_pct,
                "opening_range": opening_range,
                "setup": setup,
                "symbol": sym,
                "price": price,
                "day_pct": day_pct,
                "day_volume": data["cum_volume"],
                "day_high": data.get("day_high"),
                "rvol": rvol(data["cum_volume"], data.get("avg_volume"), now, self.cfg),
                "avg_volume": data.get("avg_volume"),
                "float_shares": data.get("float_shares"),
                "has_news": self._has_news(sym, now),
                "changes": {str(w): history.n_minute_change(now, w)
                            for w in self.cfg.gainer_windows},
            })
        return states

    def payload(self, now, require_news=None):
        cfg = self.cfg
        if require_news is not None and require_news != cfg.hod_require_news:
            cfg = replace(cfg, hod_require_news=require_news)
        states = self.build_states(now)
        qualified, near = hod.scan(states, cfg)
        stale_after = max(15.0, 5 * cfg.poll_seconds)
        is_stale = (self.last_ingest is not None
                    and (now - self.last_ingest).total_seconds() > stale_after)
        return {
            "updated": int(self.last_ingest.timestamp()) if self.last_ingest else None,
            "stale_since": int(self.last_ingest.timestamp()) if is_stale else None,
            "gainers": {str(w): top_gainers(states, w, cfg)
                        for w in cfg.gainer_windows},
            "hod": {"qualified": qualified, "near": near},
            # Rendered in the panel header. It used to be hardcoded in the
            # HTML and drifted two config changes out of date, advertising
            # $1-$20 while the bot traded $1-$5 - which reads as a broken
            # scanner rather than a stale label.
            "criteria": {
                "min_price": cfg.hod_min_price,
                "max_price": cfg.hod_max_price,
                "observe_max_price": cfg.hod_observe_max_price,
                "max_float": cfg.hod_max_float,
                "min_rvol": cfg.hod_min_rvol,
                "min_pct_up": cfg.hod_min_pct_up,
                "min_open_pct": cfg.hod_min_open_pct,
            },
            "news": self.news,
            "calendar": self.calendar,
        }
