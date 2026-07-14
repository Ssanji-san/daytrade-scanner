"""All tunables in one place.

Thresholds follow Ross Cameron's stock-selection criteria (float, % up
today, volume, relative volume, news). Change values here, not in the
scanner logic.
"""
from dataclasses import dataclass, field


@dataclass
class Config:
    # --- poll loop ---
    poll_seconds: float = 3.0          # movers/actives/snapshots cycle
    news_poll_seconds: float = 30.0
    calendar_poll_seconds: float = 600.0
    movers_top: int = 50
    actives_top: int = 100
    candidate_ttl_minutes: int = 30    # keep tracking a symbol this long after it leaves the lists

    # --- scanner 1: rolling top gainers ---
    gainer_windows: tuple = (5, 10, 15)   # minutes
    gainer_rows: int = 20

    # --- scanner 2: HOD momentum (Ross Cameron's five criteria) ---
    hod_min_price: float = 1.0
    hod_max_price: float = 20.0
    hod_max_float: float = 20_000_000   # shares (approximated by shares outstanding)
    hod_min_pct_up: float = 10.0        # % up vs previous close
    hod_min_volume: int = 100_000       # cumulative shares today (tradability floor)
    hod_min_rvol: float = 5.0           # relative volume vs 30-day average
    hod_require_news: bool = False      # UI toggle; badge always shown
    hod_near_high_pct: float = 1.0      # "at HOD" = within this % of day high
    hod_rows: int = 20
    near_filter_max_failures: int = 1   # dimmed "about to qualify" section

    # --- relative volume ---
    rvol_baseline_days: int = 30
    # Linear time-of-day adjustment floor: before this fraction of the
    # session has elapsed, treat elapsed as this to avoid absurd rvol at the open.
    rvol_min_session_fraction: float = 0.05

    # --- news ---
    news_max_age_hours: float = 24.0    # headline this recent => catalyst badge
    news_per_symbol: int = 3

    # --- calendar ---
    calendar_impacts: tuple = ("High", "Medium")  # red / orange
    calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8124

    # --- data endpoints ---
    data_base: str = "https://data.alpaca.markets"
    feed: str = "iex"                   # snapshots/bars feed on the free plan
    sec_tickers_url: str = "https://www.sec.gov/files/company_tickers.json"
    float_cache_days: int = 7
    float_cache_path: str = "cache/floats.json"


DEFAULT = Config()
