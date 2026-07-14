"""Pure trading decisions: entry gate, position sizing, 2R/3R exit levels.

No I/O here — the bot loop feeds in current state, this answers what to do.
Every threshold comes from config.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from ..config import Config

ET = ZoneInfo("America/New_York")


def _parse_hhmm(text):
    hour, minute = text.split(":")
    return int(hour), int(minute)


def in_window(now, cfg: Config):
    et = now.astimezone(ET)
    t = (et.hour, et.minute)
    return _parse_hhmm(cfg.bot_window_open) <= t <= _parse_hhmm(cfg.bot_window_close)


def should_enter(symbol="", *, price, score, trades_today, traded_symbols,
                 day_pnl, now, cfg: Config):
    """Returns (take, rejection_reasons). Empty reasons == take the trade."""
    reasons = []
    if not (cfg.bot_min_price <= price <= cfg.bot_max_price):
        reasons.append("price")
    if not in_window(now, cfg):
        reasons.append("window")
    if trades_today >= cfg.bot_max_trades_per_day:
        reasons.append("daily_cap")
    if symbol in traded_symbols:
        reasons.append("already_traded")
    if score < cfg.bot_score_threshold:
        reasons.append("score")
    if day_pnl <= -cfg.bot_bankroll * cfg.bot_daily_loss_pct / 100:
        reasons.append("kill_switch")
    return not reasons, reasons


def size_position(price, cfg: Config):
    """(shares, stop_price). Risk a fixed % of bankroll; cap the notional."""
    stop_price = price * (1 - cfg.bot_stop_pct / 100)
    risk_dollars = cfg.bot_bankroll * cfg.bot_risk_pct / 100
    qty = int(risk_dollars / (price - stop_price))
    max_notional = cfg.bot_bankroll * cfg.bot_max_notional_pct / 100
    qty = min(qty, int(max_notional / price))
    return qty, stop_price


def exit_levels(entry_price, cfg: Config):
    """Stop at -1R; targets at each configured R multiple (default 2R, 3R)."""
    r = entry_price * cfg.bot_stop_pct / 100
    return {
        "stop": entry_price - r,
        "targets": [entry_price + mult * r for mult in cfg.bot_targets_r],
    }


def split_qty(qty):
    """Split shares across the two target legs; odd share goes to the first."""
    half = qty // 2
    return qty - half, half
