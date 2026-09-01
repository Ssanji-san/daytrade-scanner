"""Entry setups: VWAP, the micro-pullback trigger, the opening range.

Pure functions over completed 1-minute bars. No I/O.

The scanners find momentum; this module decides *when* to buy it. Buying
at the high of day is chasing - the edge is the first pullback: let price
pull back one to three candles off a swing high, then buy the break of the
prior candle's high with the stop at the pullback low. That gives a
defined, tight risk instead of an arbitrary percentage.

A gapper at the open has no pullback to trade yet - there are no session
bars behind it - so it gets its own trigger: let the first few minutes
carve out a range, then buy the break of that range's high with the stop
at its low.
"""


def vwap(bars):
    """Session VWAP from typical price x volume. None until there's volume."""
    numerator = denominator = 0.0
    for bar in bars:
        typical = (bar["h"] + bar["l"] + bar["c"]) / 3.0
        numerator += typical * bar["v"]
        denominator += bar["v"]
    return numerator / denominator if denominator else None


def ema(bars, period):
    """Exponential moving average of the closes, or None if too few bars."""
    closes = [b["c"] for b in bars if b.get("c") is not None]
    if len(closes) < period:
        return None
    weight = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    for close in closes[period:]:
        value = close * weight + value * (1 - weight)
    return value


def topping_tail(bar, cfg):
    """A large upper wick: price was pushed up and sold straight back down.

    Bearish wherever it appears - it says the sellers took the level back.
    """
    if not bar:
        return False
    high, low = bar.get("h"), bar.get("l")
    open_, close = bar.get("o"), bar.get("c")
    if high is None or low is None or open_ is None or close is None:
        return False
    span = high - low
    if span <= 0:
        return False
    return 100.0 * (high - max(open_, close)) / span >= cfg.setup_topping_tail_pct


def _volume_favours_buyers(move, pullback):
    """Heavier volume driving the move up than coming back on the pullback.

    Averaged per candle rather than summed: the move is usually several bars
    and the pullback one to three, so totals would compare different things.
    Nothing to compare means nothing to object to.
    """
    ups = [b.get("v") or 0 for b in move
           if (b.get("c") or 0) >= (b.get("o") or 0)]
    downs = [b.get("v") or 0 for b in pullback
             if (b.get("c") or 0) < (b.get("o") or 0)]
    if not ups or not downs:
        return True
    # >= not >: the test is whether the sellers have MORE size, and a tie is
    # not that. Strict comparison also blocked any symbol whose bars happen
    # to carry equal volume.
    return sum(ups) / len(ups) >= sum(downs) / len(downs)


def detect_opening_range_break(opening_range, price, gap_pct, cfg):
    """The gap-and-go trigger: a gapper breaking its opening range.

    `opening_range` is {"high", "low"} frozen from the first few minutes of
    the session (see MarketState) - it is not recomputed from bars, which
    roll off a long session. Returns {setup, stop, ...} or None.
    """
    if not opening_range or not price:
        return None                      # range has not formed yet
    if (gap_pct or 0) < cfg.gap_min_pct:
        return None                      # not a gapper, this is not that trade
    high, low = opening_range.get("high"), opening_range.get("low")
    if not high or not low or low >= high:
        return None
    if price <= high:
        return None                      # still inside the range

    return {
        "setup": "opening_range",
        "stop": low,
        "swing_high": high,
        "pullback_low": low,
        "trigger": high,
    }


def detect_pullback(bars, price, cfg):
    """The micro-pullback / flat-top trigger.

    Returns {setup, stop, swing_high, pullback_low, trigger} when price is
    breaking to a new high after a shallow pullback, else None.
    """
    if len(bars) < 3 or not price:
        return None
    window = list(bars)[-cfg.setup_lookback_bars:]
    highs = [b["h"] for b in window]
    swing_idx = max(range(len(window)), key=lambda i: highs[i])
    swing_high = highs[swing_idx]
    if not swing_high:
        return None

    after = window[swing_idx + 1:]
    if not 1 <= len(after) <= cfg.setup_max_pullback_bars:
        return None                      # no pullback yet, or too deep in time

    pullback_low = min(b["l"] for b in after)
    depth = 100.0 * (swing_high - pullback_low) / swing_high
    if not cfg.setup_min_pullback_pct <= depth <= cfg.setup_max_pullback_pct:
        return None                      # noise, or the move already broke down

    # Give back more than half the move that produced it and the pullback is
    # a failure, not a flag.
    move = window[:swing_idx + 1]
    lows = [b["l"] for b in move if b.get("l") is not None]
    move_low = min(lows) if lows else None
    if move_low is not None and swing_high > move_low:
        retrace = 100.0 * (swing_high - pullback_low) / (swing_high - move_low)
        if retrace > cfg.setup_max_retrace_pct:
            return None

    if cfg.require_rising_volume and not _volume_favours_buyers(move, after):
        return None                      # the sellers are the ones with size

    if cfg.require_ema:
        # Breaking the 9 EMA invalidates the flag, as breaking VWAP does.
        # Too few bars to have an EMA yet is NOT a break: requiring one
        # blocked every setup in the first nine minutes after the bell,
        # which is exactly the opening drive this strategy is built around.
        trend = ema(bars, cfg.setup_ema_period)
        if trend is not None and pullback_low < trend:
            return None

    if cfg.setup_topping_tail_pct and any(topping_tail(b, cfg)
                                          for b in window[swing_idx:]):
        return None                      # sellers took the high back

    trigger = after[-1]["h"]
    if price <= trigger:
        return None                      # not making a new high off the flag yet

    touches = sum(1 for h in highs
                  if 100.0 * abs(h - swing_high) / swing_high
                  <= cfg.setup_flat_top_tolerance_pct)
    return {
        "setup": "flat_top" if touches >= 2 else "micro_pullback",
        "stop": pullback_low,
        "swing_high": swing_high,
        "pullback_low": pullback_low,
        "trigger": trigger,
    }
