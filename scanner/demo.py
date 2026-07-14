"""Synthetic demo session so the dashboard can be reviewed with markets closed.

Planted actors (asserted in tests/test_demo.py):
  MOVR  - spikes 10% in the final 5 minutes -> tops the rolling-gainers panel
  HODX  - $5.50 low-float (8M) runner at high of day with news -> HOD qualified
  NEARX - passes everything except relative volume -> dimmed near list
The rest are believable filler. Timestamps anchor to "now" so the demo
always looks live regardless of when it is run.
"""
import datetime as dt

from .config import Config

MINUTES = 25
STEP_SECONDS = 15


def _actors():
    """symbol -> (price_fn(minutes), static fields). Prices must be
    monotonic-or-flat so day_high == price stays consistent."""
    def movr(m):   # flat, then +2%/min for the last 5 minutes
        return 10.0 if m <= MINUTES - 5 else 10.0 * (1 + 0.02 * (m - (MINUTES - 5)))

    ramp = lambda lo, hi: (lambda m: lo + (hi - lo) * m / MINUTES)
    return {
        "MOVR":  (movr,              {"prev_close": 9.80, "final_vol": 6_000_000,
                                      "avg_volume": 5_000_000, "float_shares": 45_000_000}),
        "HODX":  (ramp(4.80, 5.50),  {"prev_close": 4.00, "final_vol": 3_000_000,
                                      "avg_volume": 400_000, "float_shares": 8_000_000}),
        "NEARX": (ramp(3.30, 3.85),  {"prev_close": 3.30, "final_vol": 150_000,
                                      "avg_volume": 3_000_000, "float_shares": 5_000_000}),
        "RUNA":  (ramp(7.90, 8.30),  {"prev_close": 7.20, "final_vol": 900_000,
                                      "avg_volume": 350_000, "float_shares": 12_000_000}),
        "RUNB":  (ramp(12.4, 12.9),  {"prev_close": 11.9, "final_vol": 1_400_000,
                                      "avg_volume": 900_000, "float_shares": 28_000_000}),
        "BIGCO": (lambda m: 210.0,   {"prev_close": 209.0, "final_vol": 30_000_000,
                                      "avg_volume": 40_000_000, "float_shares": 15_000_000_000}),
        "PENNY": (lambda m: 0.55,    {"prev_close": 0.40, "final_vol": 5_000_000,
                                      "avg_volume": 2_000_000, "float_shares": 30_000_000}),
    }


def build_demo_session(cfg: Config, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(minutes=MINUTES)
    actors = _actors()

    frames = []
    steps = MINUTES * 60 // STEP_SECONDS
    for i in range(steps + 1):
        minutes = i * STEP_SECONDS / 60
        progress = minutes / MINUTES
        symbols = {}
        for sym, (price_fn, f) in actors.items():
            price = round(price_fn(minutes), 4)
            symbols[sym] = {
                "price": price,
                "cum_volume": int(f["final_vol"] * progress),
                "day_high": price,
                "prev_close": f["prev_close"],
                "avg_volume": f["avg_volume"],
                "float_shares": f["float_shares"],
            }
        frames.append({"ts": int((start + dt.timedelta(seconds=i * STEP_SECONDS)).timestamp()),
                       "symbols": symbols})

    ts = lambda delta_min: int((now + dt.timedelta(minutes=delta_min)).timestamp())
    news = [
        {"symbol": "HODX", "headline": "HODX receives FDA approval for lead drug candidate",
         "ts": ts(-30), "url": "https://example.com/hodx", "source": "demo"},
        {"symbol": "MOVR", "headline": "MOVR surges after surprise earnings beat",
         "ts": ts(-4), "url": "https://example.com/movr", "source": "demo"},
        {"symbol": "RUNA", "headline": "RUNA announces strategic partnership",
         "ts": ts(-65), "url": "https://example.com/runa", "source": "demo"},
    ]

    iso = lambda delta_min: (now + dt.timedelta(minutes=delta_min)).isoformat()
    calendar_events = [
        {"title": "CPI y/y", "country": "USD", "impact": "High",
         "date": iso(120), "forecast": "2.9%", "previous": "3.1%"},
        {"title": "Core Retail Sales m/m", "country": "USD", "impact": "Medium",
         "date": iso(-60), "forecast": "0.3%", "previous": "0.2%"},
        {"title": "FOMC Member Speaks", "country": "USD", "impact": "Low",
         "date": iso(30), "forecast": "", "previous": ""},
    ]
    return {"frames": frames, "news": news, "calendar_events": calendar_events}
