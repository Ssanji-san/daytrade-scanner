"""Holds live market state and assembles the JSON the dashboard polls.

Both the live poll loop and replay mode push per-symbol dicts through
`ingest()`; everything downstream (history, gainers, HOD scan, news
badges) is computed here so the two modes exercise identical logic.
"""
from dataclasses import replace

from . import hod
from .config import Config
from .gainers import top_gainers
from .history import SymbolHistory, rvol


class MarketState:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.histories = {}      # symbol -> SymbolHistory
        self.latest = {}         # symbol -> last merged snapshot dict
        self.news = []
        self._news_ts = {}       # symbol -> newest headline epoch
        self.calendar = []
        self.last_ingest = None

    def ingest(self, now, symbol_data):
        """symbol_data: {sym: {price, cum_volume, day_high, prev_close,
        avg_volume, float_shares}}; avg_volume/float_shares stick once seen."""
        self.last_ingest = now
        for sym, data in symbol_data.items():
            history = self.histories.setdefault(sym, SymbolHistory())
            history.add(now, data["price"], data["cum_volume"])
            prev = self.latest.get(sym, {})
            merged = dict(data)
            for sticky in ("avg_volume", "float_shares"):
                if merged.get(sticky) is None:
                    merged[sticky] = prev.get(sticky)
            if prev.get("day_high"):
                merged["day_high"] = max(prev["day_high"], merged.get("day_high") or 0)
            self.latest[sym] = merged

    def set_news(self, now, items):
        self.news = sorted(items, key=lambda i: -i["ts"])[:100]
        self._news_ts = {}
        for item in items:
            sym = item["symbol"]
            self._news_ts[sym] = max(self._news_ts.get(sym, 0), item["ts"])

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
            states.append({
                "symbol": sym,
                "price": price,
                "day_pct": day_pct,
                "day_volume": data["cum_volume"],
                "day_high": data.get("day_high"),
                "rvol": rvol(data["cum_volume"], data.get("avg_volume"), now, self.cfg),
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
