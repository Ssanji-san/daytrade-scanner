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


def broker_state_reasons(account, notional, cfg: Config):
    """Why the broker itself would refuse this order, from /v2/account.

    Deliberately reads what Alpaca reports rather than re-implementing the
    day-trading rules: the old "$25k or 3 day trades a week" regime is being
    replaced by real-time margin exposure, and broker rollout runs into 2027,
    so any regulation hardcoded here would be wrong for someone. A flagged
    account has its day-trading buying power computed by the broker; we just
    read the number.

    A missing or unreadable snapshot returns no reasons. Failing open keeps a
    network hiccup from halting the session - a genuinely refused order is
    already handled once, by TradingBot.rejected.
    """
    if not account:
        return []
    reasons = []
    if account.get("trading_blocked") or account.get("account_blocked"):
        reasons.append("broker_blocked")
    if account.get("buying_power") is None or not notional:
        return reasons           # nothing to compare against; fail open
    try:
        power = float(account["buying_power"])
        if account.get("pattern_day_trader"):
            power = min(power,
                        float(account.get("daytrading_buying_power") or 0))
    except (TypeError, ValueError):
        return reasons
    if power < notional:
        reasons.append("buying_power")
    return reasons


def should_enter(symbol="", *, price, score, trades_today, traded_symbols,
                 day_pnl, now, cfg: Config, score_threshold=None,
                 losses_today=0, open_positions=0, account=None,
                 notional=None):
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
    if losses_today >= cfg.bot_max_losses_per_day:
        reasons.append("loss_cap")
    if open_positions >= cfg.bot_max_concurrent_positions:
        reasons.append("concurrency")
    if symbol in traded_symbols:
        reasons.append("already_traded")
    if score < threshold:
        reasons.append("score")
    # 0 disables the dollar rule; the loss count is the kill switch now.
    if (cfg.bot_daily_loss_pct > 0
            and day_pnl <= -cfg.bot_bankroll * cfg.bot_daily_loss_pct / 100):
        reasons.append("kill_switch")
    reasons.extend(broker_state_reasons(account, notional, cfg))
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
    # Two ceilings: a share of the account, and a hard dollar figure the
    # position never exceeds however large the account grows.
    max_notional = cfg.bot_bankroll * cfg.bot_max_notional_pct / 100
    cap = getattr(cfg, "bot_max_notional_dollars", 0)
    if cap:
        max_notional = min(max_notional, cap)
    qty = min(qty, int(max_notional / price))
    return qty, stop_price


def is_doji(bar, cfg: Config):
    """A bar that opened and closed in the same place: nobody winning.

    Two of these in a row is the stall this strategy exits on. A bar with no
    range at all is the purest version of it.
    """
    if not bar:
        return False
    open_, close = bar.get("o"), bar.get("c")
    high, low = bar.get("h"), bar.get("l")
    if open_ is None or close is None or high is None or low is None:
        return False
    span = high - low
    if span <= 0:
        return True
    return abs(close - open_) <= (cfg.bot_doji_body_pct / 100.0) * span


def scalp_levels(entry_price, cfg: Config):
    """Stop a fixed % below, target a fixed number of cents above."""
    stop = entry_price * (1 - cfg.bot_stop_pct / 100)
    return {"stop": round(stop, 2),
            "target": round(entry_price + cfg.bot_scalp_target_cents, 2)}


def scalp_split(qty, cfg: Config):
    """(banked, runner) shares at the target. Runner may be zero."""
    if cfg.bot_scalp_scale_out_pct >= 100:
        return qty, 0
    banked = int(qty * cfg.bot_scalp_scale_out_pct / 100.0)
    banked = max(0, min(qty, banked))
    return banked, qty - banked


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
