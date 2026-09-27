"""Pure trading decisions: entry gate, position sizing, scalp exit levels.

No I/O here - the bot loop feeds in current state, this answers what to do.
Every threshold comes from config.

The live strategy is Ross Cameron's cents-on-the-dollar scalp: buy the
pullback, bank most of the position a fixed number of cents up, and let the
rest ride a trail that can never come back under what was paid. The R-based
2R/3R path is still here and still reachable by configuration.
"""
import math
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
    power = buying_power(account)
    if power is None or not notional:
        return reasons           # nothing to compare against; fail open
    if power < notional:
        reasons.append("buying_power")
    return reasons


def buying_power(account):
    """What the broker says can be spent right now, or None if unreadable.

    A flagged account has its day-trading buying power computed by the
    broker; we read the number rather than re-deriving a rule that is being
    replaced through 2027.
    """
    if not account or account.get("buying_power") is None:
        return None
    try:
        power = float(account["buying_power"])
        if account.get("pattern_day_trader"):
            power = min(power,
                        float(account.get("daytrading_buying_power") or 0))
    except (TypeError, ValueError):
        return None
    return power


def should_enter(symbol="", *, price, score, trades_today, traded_symbols,
                 day_pnl, now, cfg: Config, score_threshold=None,
                 losses_today=0, open_positions=0, account=None,
                 notional=None, bankroll=None):
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
            and day_pnl <= -(bankroll or cfg.bot_bankroll)
            * cfg.bot_daily_loss_pct / 100):
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


def bankroll_from(account, cfg: Config, last_known=None):
    """The balance to work with: the live account, not a hardcoded figure.

    It decides how many positions fit, not how big each one is - see
    position_slots and size_position. Falls back to the configured bankroll
    when the account cannot be read, and refuses a reading more than 3x the
    last one, because a bad parse must never decide what gets opened.

    `last_known=None` means "no reading yet, take this one as the baseline".
    Passing the config seed there instead would measure the first real
    reading against a number that was never a balance: a $4,000 account
    tripped the 3x guard against the $1,000 seed on every single cycle and
    stayed pinned at $1,000 forever.

    The backtest deliberately does NOT use this: a replay that compounds
    would overstate its own results and make R multiples incomparable
    between the start of the sample and the end.
    """
    base = last_known or cfg.bot_bankroll
    if not account:
        return base
    try:
        equity = float(account.get("equity"))
    except (TypeError, ValueError):
        return base
    if equity <= 0 or (last_known and equity > 3 * last_known):
        return base
    return equity


def position_slots(bankroll, cfg: Config):
    """How many positions this balance supports, ceiling included.

    $2,473.74 holds two whole $1,000 units plus a $473.74 slice - three
    positions. A slice under bot_min_position_dollars does not count.
    """
    unit = cfg.bot_position_dollars
    if not bankroll or unit <= 0:
        return 0
    whole = int(bankroll // unit)
    if bankroll - whole * unit >= cfg.bot_min_position_dollars:
        whole += 1
    return max(0, min(whole, cfg.bot_max_concurrent_positions))


def size_position(price, cfg: Config, stop_price=None, unit=None, budget=None):
    """(shares, stop_price) for ONE position.

    `unit` is what a whole position is worth - deliberately not the account.
    The account decides how many units fit; see TradingBot.cycle. `budget`
    caps this one at the capital actually left, which is what lets the last
    slice of a balance open a part-sized position instead of being refused
    outright by the buying-power check.
    """
    if stop_price is None:
        stop_price = price * (1 - cfg.bot_stop_pct / 100)
    if stop_price >= price:
        return 0, stop_price
    unit = cfg.bot_position_dollars if unit is None else unit
    risk_dollars = unit * cfg.bot_risk_pct / 100
    qty = int(risk_dollars / (price - stop_price))
    # Three ceilings: a share of the unit, a hard dollar figure no position
    # ever exceeds, and the capital that is actually left to spend.
    max_notional = unit * cfg.bot_max_notional_pct / 100
    cap = getattr(cfg, "bot_max_notional_dollars", 0)
    if cap:
        max_notional = min(max_notional, cap)
    if budget is not None:
        max_notional = min(max_notional, budget)
    if max_notional < cfg.bot_min_position_dollars:
        return 0, stop_price      # too small to pay for its own spread
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


def runner_trail_pct(entry, price, cfg: Config):
    """Trail width for the runner, capped so it never starts below entry.

    A flat 5% trail on a $5 entry puts the first stop at $4.94 - below what
    was paid - so a banked winner could still hand the runner back as a loss,
    which is the whole thing scaling out exists to prevent. The cap is the
    distance from here back to the entry.

    None means the cap has collapsed (price at or under the entry) and the
    caller should place a plain stop at break-even instead.
    """
    if not entry or not price or price <= entry:
        return None
    cap = 100.0 * (price - entry) / price
    # Floor, never round: rounding up widens the trail past break-even, which
    # is exactly the loss this cap exists to make impossible.
    pct = math.floor(min(cfg.bot_runner_trail_pct, cap) * 100) / 100
    return pct if pct > 0 else None


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
