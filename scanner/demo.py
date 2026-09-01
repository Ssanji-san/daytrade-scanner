"""Synthetic demo session so the dashboard can be reviewed with markets closed.

Planted actors (asserted in tests/test_demo.py):
  MOVR  - spikes 10% in the final 5 minutes -> tops the rolling-gainers panel
  HODX  - low-float (8M) $3 runner at high of day with news -> HOD qualified,
          and it flags before it breaks out, so the pullback entry has
          something to fire on
  NEARX - passes everything except relative volume -> dimmed near list
The rest are believable filler. Timestamps anchor to "now" so the demo
always looks live regardless of when it is run.
"""
import datetime as dt

from .config import Config
from .history import ET
from .trading.strategy import position_slots

MINUTES = 25
STEP_SECONDS = 15


def _actors():
    """symbol -> (price_fn(minutes), static fields). Prices must be
    monotonic-or-flat so day_high == price stays consistent."""
    def movr(m):   # flat, then +2%/min for the last 5 minutes
        return 10.0 if m <= MINUTES - 5 else 10.0 * (1 + 0.02 * (m - (MINUTES - 5)))

    def hodx(m):
        """Runs, flags for a few minutes, then breaks out.

        This is the shape the bot waits for: it must NOT buy the initial
        ramp, only the break of the flag.
        """
        run, flag = MINUTES - 7, MINUTES - 4
        if m <= run:
            return 2.80 + (3.50 - 2.80) * m / run
        if m <= flag:
            return 3.50 - 0.14 * (m - run) / (flag - run)      # pull back
        return 3.36 + 0.34 * (m - flag) / (MINUTES - flag)     # new high

    ramp = lambda lo, hi: (lambda m: lo + (hi - lo) * m / MINUTES)
    return {
        "MOVR":  (movr,              {"prev_close": 9.80, "final_vol": 6_000_000,
                                      "avg_volume": 5_000_000, "float_shares": 45_000_000}),
        "HODX":  (hodx,            {"prev_close": 2.40, "final_vol": 3_000_000,
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
    # Anchored to 09:55 ET rather than the wall clock. The opening-drive
    # criterion only means anything after the bell, so a demo built at 6am
    # would show every symbol failing for a reason that is about the clock
    # rather than the data. Starting 25 minutes earlier puts the first bars
    # in premarket, which is where a real session starts too.
    if now is None:
        now = dt.datetime.now(ET).replace(hour=9, minute=55, second=0,
                                          microsecond=0)
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
            bar_ts = (start + dt.timedelta(seconds=i * STEP_SECONDS)
                      ).replace(second=0, microsecond=0).isoformat()
            symbols[sym] = {
                "price": price,
                "cum_volume": int(f["final_vol"] * progress),
                "day_high": price,
                "prev_close": f["prev_close"],
                "avg_volume": f["avg_volume"],
                "float_shares": f["float_shares"],
                "minute_bar": {"t": bar_ts, "o": price, "h": price,
                               "l": round(price * 0.995, 4), "c": price,
                               "v": max(1, int(f["final_vol"] / 100))},
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
        # Dilution: looks like news, kills the runner. The bot must veto it.
        {"symbol": "RUNB", "headline": "RUNB announces pricing of $40M "
                                       "underwritten public offering",
         "ts": ts(-12), "url": "https://example.com/runb", "source": "demo"},
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


def build_demo_bot_status(cfg: Config, now=None):
    """Fake bot state so the Bot panel is reviewable without keys or markets."""
    now = now or dt.datetime.now(dt.timezone.utc)
    ts = lambda mins_ago: int((now - dt.timedelta(minutes=mins_ago)).timestamp())

    def trade(mins_ago, symbol, qty, entry, exit_price, reason, held_min=12):
        risk = entry * cfg.bot_stop_pct / 100
        return {"ts": ts(mins_ago), "symbol": symbol, "qty": qty,
                "entry": entry, "exit_ts": ts(mins_ago - held_min),
                "exit_price": exit_price,
                "pnl": round((exit_price - entry) * qty, 2),
                "r_multiple": round((exit_price - entry) / risk, 2),
                "exit_reason": reason,
                "setup": "micro_pullback" if qty % 2 else "flat_top"}

    today = [
        trade(180, "RUNA", 30, 8.10, 9.32, "trailing"),     # runner trailed ~+5R
        trade(150, "NEARX", 64, 3.90, 3.78, "stop"),        # -1R
        trade(95, "HODX", 46, 5.35, 5.67, "trailing"),      # banked +2R, trailed out
    ]
    day_pnl = round(sum(t["pnl"] for t in today), 2)
    base = cfg.bot_bankroll
    equity = []
    for day_offset in range(20, -1, -1):
        drift = (20 - day_offset) * 14 + (day_offset % 3 - 1) * 35
        equity.append([int((now - dt.timedelta(days=day_offset)).timestamp()),
                       round(base + drift, 2)])

    return {
        "enabled": True, "error": None,
        "bankroll": cfg.bot_bankroll,
        "position_dollars": cfg.bot_position_dollars,
        "slots": position_slots(cfg.bot_bankroll, cfg),
        "target_cents": (cfg.bot_scalp_target_cents
                         if cfg.bot_scalp_mode else None),
        "trades_today": len(today), "cap": cfg.bot_max_trades_per_day,
        "day_pnl": day_pnl,
        "open": [{"symbol": "MOVR", "qty": 22, "entry": 11.02,
                  "opened_ts": ts(6)}],
        "today": today,
        "recent": today,
        "stats": {"count": 17, "win_rate": 0.47, "expectancy_r": 0.42},
        "model": {"kind": "logreg", "samples": 63, "holdout_acc": 0.62},
        "model_history": [
            {"ts": ts(60 * 24), "samples": 63, "holdout_acc": 0.62},
            {"ts": ts(60 * 48), "samples": 41, "holdout_acc": 0.55},
        ],
        "equity": equity,
        "orders": [
            {"symbol": "MOVR", "side": "sell", "type": "trailing_stop",
             "qty": 11, "limit_price": None, "stop_price": "10.47",
             "status": "held"},
            {"symbol": "HODX", "side": "buy", "type": "limit", "qty": 46,
             "limit_price": "5.37", "stop_price": "5.19", "status": "new"},
        ],
        "alerts": [
            {"ts": ts(20), "day": "2026-08-18", "symbol": "MOVR",
             "price": 11.02, "setup": "micro_pullback", "label": None},
            {"ts": ts(75), "day": "2026-08-18", "symbol": "HODX",
             "price": 5.35, "setup": "flat_top", "label": 1},
            {"ts": ts(140), "day": "2026-08-18", "symbol": "NEARX",
             "price": 3.90, "setup": "micro_pullback", "label": 0},
            {"ts": ts(190), "day": "2026-08-17", "symbol": "RUNA",
             "price": 8.10, "setup": "micro_pullback", "label": 1},
        ],
        "setup_stats": [
            {"setup": "micro_pullback", "n": 11, "wins": 6, "exp_r": 0.48},
            {"setup": "flat_top", "n": 6, "wins": 2, "exp_r": -0.21},
        ],
    }
