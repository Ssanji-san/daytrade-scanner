"""Float approximation from SEC EDGAR shares outstanding, cached to disk.

True float needs paid data; shares outstanding (dei:
EntityCommonStockSharesOutstanding via the companyconcept API) is a free,
close-enough upper bound. The UI labels it with an approx sign.
"""
import datetime as dt
import json
import pathlib

from .config import Config


def parse_ticker_map(payload):
    """SEC company_tickers.json -> {ticker: cik}."""
    return {row["ticker"]: row["cik_str"] for row in payload.values()}


def parse_shares(concept_payload):
    """Latest reported shares outstanding from a companyconcept payload."""
    entries = (concept_payload.get("units") or {}).get("shares") or []
    if not entries:
        return None
    return max(entries, key=lambda e: e.get("end", ""))["val"]


class FloatCache:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = pathlib.Path(cfg.float_cache_path)
        self._data = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, symbol):
        entry = self._data.get(symbol)
        return entry["shares"] if entry else None

    def put(self, symbol, shares, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        self._data[symbol] = {"shares": shares, "fetched": now.isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data), encoding="utf-8")

    def is_stale(self, symbol, now=None):
        entry = self._data.get(symbol)
        if not entry:
            return True
        now = now or dt.datetime.now(dt.timezone.utc)
        fetched = dt.datetime.fromisoformat(entry["fetched"])
        return (now - fetched).days > self.cfg.float_cache_days
