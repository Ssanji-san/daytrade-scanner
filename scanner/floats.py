"""Float approximation from SEC EDGAR shares outstanding, cached to disk.

True float needs paid data; shares outstanding (dei:
EntityCommonStockSharesOutstanding via the companyconcept API) is a free,
close-enough upper bound. The UI labels it with an approx sign.
"""
import asyncio
import datetime as dt
import json
import os
import pathlib

from .config import Config

# SEC asks automated clients to identify themselves.
SEC_HEADERS = {"User-Agent": os.environ.get(
    "SEC_CONTACT", "daytrade-scanner personal-use")}


async def fetch_ticker_map(session, cfg: Config):
    async with session.get(cfg.sec_tickers_url, headers=SEC_HEADERS) as resp:
        resp.raise_for_status()
        return parse_ticker_map(await resp.json())


# Companies file shares outstanding under more than one taxonomy. Trying
# only the dei concept threw away every issuer that reports the us-gaap one.
SHARE_CONCEPTS = (
    "dei/EntityCommonStockSharesOutstanding",
    "us-gaap/CommonStockSharesOutstanding",
    "us-gaap/CommonStockSharesIssued",
)


async def fetch_shares(session, cik):
    """(shares, answered). `answered` is False when the REQUEST failed.

    The distinction is the whole point. SEC returning 404 means this issuer
    genuinely does not file that concept - worth remembering. A 403, a 429
    or a timeout means we simply did not get an answer, and remembering
    THAT as "no float" locks the stock out for a week over a moment's rate
    limiting. An unknown float is an automatic rejection, so a cached
    failure is indistinguishable from a company that does not exist.
    """
    answered = False
    for i, concept in enumerate(SHARE_CONCEPTS):
        if i:
            # Only reached when the previous taxonomy 404'd. SEC throttles
            # at 10 requests a second and answers 403 when crossed - which
            # is what filled a fifth of the cache with false "no float"
            # verdicts in the first place.
            await asyncio.sleep(0.12)
        url = ("https://data.sec.gov/api/xbrl/companyconcept/"
               f"CIK{cik:010d}/{concept}.json")
        try:
            async with session.get(url, headers=SEC_HEADERS) as resp:
                if resp.status == 404:
                    answered = True      # SEC spoke: not under this taxonomy
                    continue
                if resp.status != 200:
                    return None, False   # 403 / 429 / 5xx - ask again later
                shares = parse_shares(await resp.json(content_type=None))
        except Exception:
            return None, False
        if shares:
            return shares, True
        answered = True
    return None, answered


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
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                # A half-written cache must not take the session down with
                # it: this runs during live_loop's setup, outside its error
                # handling, so the whole scanner died with it. Refetching is
                # cheap; the file is rebuilt as symbols come back round.
                print(f"[warn] unreadable float cache, starting empty: {exc}")

    def get(self, symbol):
        entry = self._data.get(symbol)
        return entry["shares"] if entry else None

    def put(self, symbol, shares, now=None, answered=True, flush=True):
        """Record a lookup. `flush=False` defers the write to save().

        The live loop fetches four symbols a cycle and wants each one on
        disk immediately. A bulk population does thousands, and rewriting a
        megabyte-sized file per symbol would be gigabytes of pointless IO.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        self._data[symbol] = {"shares": shares, "fetched": now.isoformat(),
                              "answered": bool(answered)}
        if flush:
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Whole-file rewrite on every symbol, so write beside it and rename:
        # a cancelled workflow otherwise leaves a truncated cache behind.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        os.replace(tmp, self.path)

    def is_stale(self, symbol, now=None):
        entry = self._data.get(symbol)
        if not entry:
            return True
        now = now or dt.datetime.now(dt.timezone.utc)
        age = now - dt.datetime.fromisoformat(entry["fetched"])
        if entry.get("shares") is None and not entry.get("answered"):
            # A miss we never got an answer for. Retry within the hour
            # instead of writing the stock off for a week. Entries from
            # before this distinction have no "answered" key and are retried
            # too, which clears the poisoned ones on their own.
            return age >= dt.timedelta(minutes=self.cfg.float_retry_minutes)
        return age.days > self.cfg.float_cache_days
