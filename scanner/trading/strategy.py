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
                 day_pnl, now, cfg: Config, score_threshold=None):
    """Returns (take, rejection_reasons). Empty reasons == take the trade.

    `score_threshold` overrides the config bar once a trained model sets
    its own; see Config.bot_score_percentile.
    """
    reasons = []
    threshold = (cfg.bot_score_threshold if score_threshold is None
                 else score_threshold)
    if not (cfg.bot_min_price <= price <= cfg.bot_max_price):
        reasons.append("price")
    if not in_window(now, cfg):
        reasons.append("window")
    if trades_today >= cfg.bot_max_trades_per_day:
        reasons.append("daily_cap")
    if symbol in traded_symbols:
        reasons.append("already_traded")
    if score < threshold:
        reasons.append("score")
    if day_pnl <= -cfg.bot_bankroll * cfg.bot_daily_loss_pct / 100:
        reasons.append("kill_switch")
    return not reasons, reasons


def technical_stop(entry, raw_stop, cfg: Config):
    """Clamp a setup's stop into a sane risk band. None means don't trade.

    The stop belongs at the setup's invalidation level (the pullback low),
    not at an arbitrary percentage - but a stop tighter than noise gets
    shaken out, and one too far away is not a trade worth taking.
    """
    if not entry:
        return None
    if raw_stop is None or raw_stop >= entry:
        return entry * (1 - cfg.bot_stop_pct / 100)
    pct = 100.0 * (entry - raw_stop) / entry
    if pct > cfg.bot_max_stop_pct:
        return None
    if pct < cfg.bot_min_stop_pct:
        return entry * (1 - cfg.bot_min_stop_pct / 100)
    return raw_stop


def size_position(price, cfg: Config, stop_price=None):
    """(shares, stop_price). Risk a fixed % of bankroll; cap the notional."""
    if stop_price is None:
        stop_price = price * (1 - cfg.bot_stop_pct / 100)
    if stop_price >= price:
        return 0, stop_price
    risk_dollars = cfg.bot_bankroll * cfg.bot_risk_pct / 100
    qty = int(risk_dollars / (price - stop_price))
    max_notional = cfg.bot_bankroll * cfg.bot_max_notional_pct / 100
    qty = min(qty, int(max_notional / price))
    return qty, stop_price


def exit_levels(entry_price, cfg: Config, stop_price=None):
    """Stop at the setup low (-1R); scale-out at +scale_out_r R above entry."""
    if stop_price is None:
        stop_price = entry_price * (1 - cfg.bot_stop_pct / 100)
    r = entry_price - stop_price
    # Round to cents: these are the prices we actually send to the broker,
    # and comparing raw floats against a quoted price is a coin flip.
    return {
        "stop": round(stop_price, 2),
        "scale_out": round(entry_price + cfg.bot_scale_out_r * r, 2),
    }


def weighted_exit(legs):
    """Share-weighted average exit price. legs: list of (qty, price)."""
    total_qty = sum(qty for qty, _ in legs)
    if not total_qty:
        return None
    return sum(qty * price for qty, price in legs) / total_qty


def split_qty(qty):
    """Split shares across the two target legs; odd share goes to the first."""
    half = qty // 2
    return qty - half, half
