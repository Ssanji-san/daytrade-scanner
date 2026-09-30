"""Entry setups: VWAP, the micro-pullback trigger, the opening range.

Pure functions over completed 1-minute bars. No I/O.

The scanners find momentum; this module decides *when* to buy it. Buying
at the high of day is chasing - the edge is the first pullback: let price
pull back one to three candles off a swing high, then buy the break of the
prior candle's high with the stop at the pullback low. That gives a
defined, tight risk instead of an arbitrary percentage.

Before buying, it also helps to know how much room there is overhead:
`resistance_room` measures the distance to the ceilings Ross watches.

A gapper at the open has no pullback to trade yet - there are no session
bars behind it - so it gets its own trigger: let the first few minutes
carve out a range, then buy the break of that range's high with the stop
at its low.
"""


import math


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
    shape = _swing_and_pullback(bars, cfg)
    if shape is None:
        return None                      # no pullback yet, or too deep in time
    window, swing_idx, after, pullback_low = shape
    highs = [b["h"] for b in window]
    swing_high = highs[swing_idx]
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


def _swing_and_pullback(bars, cfg):
    """(window, swing_idx, pullback bars, pullback low), or None.

    The shared shape of both pullback entries: a swing high inside the
    lookback, then 1-3 candles pulling back a sane distance off it.
    """
    window = list(bars)[-cfg.setup_lookback_bars:]
    highs = [b["h"] for b in window]
    swing_idx = max(range(len(window)), key=lambda i: highs[i])
    swing_high = highs[swing_idx]
    if not swing_high:
        return None
    after = window[swing_idx + 1:]
    if not 1 <= len(after) <= cfg.setup_max_pullback_bars:
        return None
    return window, swing_idx, after, min(b["l"] for b in after)


def detect_dip_green(bars, current_bar, price, cfg):
    """Buy the dip: the first green candle after a red pullback.

    Earlier than `detect_pullback`, which waits for price to clear the prior
    candle's high. Here the pullback's last candle closed red and the
    current one has turned green - price above its open and above the red
    candle's close. The stop is the lowest point of the dip, including a
    wick in the green candle itself.

    Live, `current_bar` is the newest minute bar the snapshot carries; in the
    replay it is the whole minute, so the entry lands at that minute's close
    - later and usually worse than live, never better.
    """
    if len(bars) < 3 or not price or not current_bar:
        return None
    shape = _swing_and_pullback(bars, cfg)
    if shape is None:
        return None
    window, swing_idx, after, pullback_low = shape
    swing_high = window[swing_idx]["h"]

    last = after[-1]
    if not last.get("c") or not last.get("o") or last["c"] >= last["o"]:
        return None                      # the dip had already turned
    opened = current_bar.get("o")
    if not opened or price <= opened or price <= last["c"]:
        return None                      # not green yet

    low = min(pullback_low, current_bar.get("l") or pullback_low)
    depth = 100.0 * (swing_high - low) / swing_high
    if not cfg.setup_min_pullback_pct <= depth <= cfg.setup_max_pullback_pct:
        return None                      # noise, or the move broke down
    return {
        "setup": "dip_green",
        "stop": low,
        "swing_high": swing_high,
        "pullback_low": low,
        "trigger": opened,
    }


def _room(price, level):
    """Distance up to a ceiling, or None if there is none above price."""
    if not level or level <= price:
        return None                      # unknown, or already broken
    return round(level - price, 4)


def resistance_room(price, prev_high=None, premarket_high=None):
    """How far price can run before each ceiling Ross watches.

    Yesterday's high, the pre-market high, and the next half dollar -
    round numbers where sellers stack their orders. A level at or below
    price is already broken and is not resistance. Measured and journalled
    only: whether a ceiling inside the +20c target costs money is a
    question for the backtest, not an assumption.
    """
    # Strictly above: a stock sitting on $2.00 looks to $2.50. The epsilon
    # keeps 2.00 * 2 from flooring to 3.999... and skipping a level.
    half_dollar = (math.floor(price * 2 + 1e-9) + 1) / 2
    return {
        "room_prev_high": _room(price, prev_high),
        "room_premarket_high": _room(price, premarket_high),
        "room_half_dollar": round(half_dollar - price, 4),
    }
