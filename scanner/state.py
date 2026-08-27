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
        self.news = sorted(items, key=lambda i: -i["ts"])[:100]
        self._news_ts = {}
        self._news_by_symbol = {}
        for item in items:
            sym = item["symbol"]
            self._news_ts[sym] = max(self._news_ts.get(sym, 0), item["ts"])
            self._news_by_symbol.setdefault(sym, []).append(item)

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
            "news": self.news,
            "calendar": self.calendar,
        }
