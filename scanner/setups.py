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
