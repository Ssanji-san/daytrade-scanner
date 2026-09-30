"""The entry setups: VWAP and the micro-pullback trigger.

These encode the difference between chasing a high and trading a pullback,
so they get tested as carefully as the risk math.
"""
from dataclasses import replace

import pytest

from scanner.config import Config
from scanner.setups import (detect_dip_green, detect_opening_range_break,
                            detect_pullback, resistance_room, vwap)
from scanner.trading.strategy import technical_stop

CFG = Config()
# The live config collapses the stop band to a flat 20% (bot_min_stop_pct).
# These tests exercise the clamping logic itself, so they keep a real band.
BAND = replace(CFG, bot_stop_pct=3.0, bot_min_stop_pct=1.0,
               bot_max_stop_pct=6.0)


def bar(o, h, l, c, v=10_000, t="t"):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


class TestVwap:
    def test_volume_weighted_not_simple_average(self):
        bars = [bar(10, 10, 10, 10, v=1_000),      # typical 10, small size
                bar(20, 20, 20, 20, v=9_000)]      # typical 20, big size
        assert vwap(bars) == pytest.approx(19.0)   # not 15

    def test_none_without_volume(self):
        assert vwap([]) is None
        assert vwap([bar(10, 10, 10, 10, v=0)]) is None


def rally_then_pullback():
    """Runs to 5.50, pulls back two candles, sits just under the highs."""
    return [
        bar(5.00, 5.10, 4.98, 5.08),
        bar(5.08, 5.30, 5.05, 5.28),
        bar(5.28, 5.50, 5.25, 5.48),   # swing high 5.50
        bar(5.48, 5.49, 5.35, 5.38),   # red
        bar(5.38, 5.42, 5.36, 5.40),   # red-ish, pullback low 5.35
    ]


class TestPullbackTrigger:
    def test_fires_when_price_breaks_the_prior_candle_high(self):
        setup = detect_pullback(rally_then_pullback(), price=5.43, cfg=CFG)
        assert setup is not None
        assert setup["setup"] in ("micro_pullback", "flat_top")
        assert setup["pullback_low"] == pytest.approx(5.35)
        assert setup["stop"] == pytest.approx(5.35)    # stop at the flag low

    def test_silent_while_price_is_still_inside_the_pullback(self):
        # No new high yet -> no entry. This is the anti-chasing rule.
        assert detect_pullback(rally_then_pullback(), price=5.40, cfg=CFG) is None

    def test_no_setup_when_price_is_running_with_no_pullback(self):
        bars = [bar(5.0, 5.1, 5.0, 5.1), bar(5.1, 5.3, 5.1, 5.3),
                bar(5.3, 5.6, 5.3, 5.6)]      # swing high is the last bar
        assert detect_pullback(bars, price=5.70, cfg=CFG) is None

    def test_rejects_a_pullback_that_broke_down(self):
        bars = rally_then_pullback()
        bars[-1] = bar(5.38, 5.42, 4.60, 5.40)     # -16% off the high
        assert detect_pullback(bars, price=5.43, cfg=CFG) is None

    def test_rejects_noise_too_shallow_to_be_a_pullback(self):
        bars = [bar(5.00, 5.10, 4.98, 5.08), bar(5.08, 5.30, 5.05, 5.28),
                bar(5.28, 5.50, 5.25, 5.48), bar(5.48, 5.49, 5.49, 5.49)]
        assert detect_pullback(bars, price=5.55, cfg=CFG) is None

    def test_needs_history(self):
        assert detect_pullback([bar(5, 5, 5, 5)], price=6.0, cfg=CFG) is None

    def test_flat_top_when_the_high_is_tested_twice(self):
        bars = [bar(5.28, 5.50, 5.25, 5.48),
                bar(5.48, 5.50, 5.40, 5.45),   # second touch of 5.50
                bar(5.45, 5.47, 5.38, 5.44)]
        setup = detect_pullback(bars, price=5.49, cfg=CFG)
        assert setup and setup["setup"] == "flat_top"


class TestTechnicalStop:
    def test_uses_the_setup_low(self):
        assert technical_stop(5.48, 5.35, BAND) == pytest.approx(5.35)

    def test_widens_a_stop_tighter_than_noise(self):
        stop = technical_stop(10.0, 9.98, BAND)         # 0.2% -> floor 1%
        assert stop == pytest.approx(9.90)

    def test_refuses_a_setup_whose_risk_is_too_wide(self):
        assert technical_stop(10.0, 9.00, BAND) is None  # 10% > 6% max

    def test_falls_back_when_there_is_no_usable_low(self):
        assert technical_stop(10.0, None, BAND) == pytest.approx(9.70)
        assert technical_stop(10.0, 10.5, BAND) == pytest.approx(9.70)

    def test_live_config_forces_a_flat_5_percent(self):
        # min and max both at 5 collapse the band: the setup low is ignored
        # and every trade risks the same 5% of the money at work - $50 on
        # $1,000, at any share price.
        assert technical_stop(10.0, 9.85, CFG) == pytest.approx(9.50)
        assert technical_stop(10.0, None, CFG) == pytest.approx(9.50)
        assert technical_stop(10.0, 7.00, CFG) is None   # 30% > 5% max


class TestOpeningRangeBreak:
    """A gapper at the open has no flag yet - it gets the range break."""

    RANGE = {"high": 15.00, "low": 13.20}

    def test_fires_on_a_break_above_the_range_high(self):
        setup = detect_opening_range_break(self.RANGE, price=15.10,
                                           gap_pct=180.0, cfg=CFG)
        assert setup is not None
        assert setup["setup"] == "opening_range"
        assert setup["stop"] == pytest.approx(13.20)   # stop at the range low
        assert setup["trigger"] == pytest.approx(15.00)

    def test_silent_inside_the_range(self):
        assert detect_opening_range_break(self.RANGE, price=14.50,
                                          gap_pct=180.0, cfg=CFG) is None

    def test_silent_when_it_did_not_gap(self):
        # A range break on a stock that did not gap is not this trade.
        assert detect_opening_range_break(self.RANGE, price=15.10,
                                          gap_pct=2.0, cfg=CFG) is None

    def test_none_before_the_range_has_formed(self):
        assert detect_opening_range_break(None, price=15.10,
                                          gap_pct=180.0, cfg=CFG) is None

    def test_rejects_a_degenerate_range(self):
        flat = {"high": 15.0, "low": 15.0}
        assert detect_opening_range_break(flat, price=15.1,
                                          gap_pct=180.0, cfg=CFG) is None

    def test_the_range_low_still_has_to_pass_the_risk_band(self):
        # A 12% range is a real setup but too wide to risk; technical_stop
        # is what rejects it, so the two must agree.
        wide = detect_opening_range_break({"high": 15.0, "low": 13.2},
                                          price=15.1, gap_pct=180.0, cfg=CFG)
        assert technical_stop(15.1, wide["stop"], BAND) is None


class TestResistanceRoom:
    """How far price can run before the next ceiling: yesterday's high, the
    pre-market high, the next half dollar. Recorded, not yet acted on."""

    def test_distance_to_each_ceiling_above(self):
        room = resistance_room(2.05, prev_high=2.15, premarket_high=2.40)
        assert room["room_prev_high"] == pytest.approx(0.10)
        assert room["room_premarket_high"] == pytest.approx(0.35)
        assert room["room_half_dollar"] == pytest.approx(0.45)   # 2.50

    def test_a_ceiling_already_broken_is_not_resistance(self):
        room = resistance_room(2.60, prev_high=2.15, premarket_high=2.60)
        assert room["room_prev_high"] is None
        assert room["room_premarket_high"] is None   # at it = broken

    def test_unknown_levels_are_none(self):
        room = resistance_room(2.05)
        assert room["room_prev_high"] is None
        assert room["room_premarket_high"] is None

    def test_half_dollar_just_above(self):
        assert resistance_room(1.95)["room_half_dollar"] == pytest.approx(0.05)
        assert resistance_room(3.49)["room_half_dollar"] == pytest.approx(0.01)

    def test_sitting_on_a_half_dollar_looks_to_the_next_one(self):
        assert resistance_room(2.00)["room_half_dollar"] == pytest.approx(0.50)
        assert resistance_room(2.50)["room_half_dollar"] == pytest.approx(0.50)


def red_pullback():
    """Runs to 5.50, then two red candles down to 5.33."""
    return [
        bar(5.00, 5.10, 4.98, 5.08),
        bar(5.08, 5.30, 5.05, 5.28),
        bar(5.28, 5.50, 5.25, 5.48),   # swing high 5.50
        bar(5.48, 5.49, 5.35, 5.38),   # red
        bar(5.38, 5.40, 5.33, 5.36),   # red, pullback low 5.33
    ]


GREEN = bar(5.36, 5.40, 5.35, 5.39)    # the first candle to turn up


class TestDipGreen:
    """Buy the dip: the first green candle after red pullback candles.

    Earlier than the break entry, which waits for price to clear the prior
    candle's high."""

    def test_fires_on_the_first_green_candle(self):
        setup = detect_dip_green(red_pullback(), GREEN, price=5.39, cfg=CFG)
        assert setup is not None
        assert setup["setup"] == "dip_green"
        assert setup["stop"] == pytest.approx(5.33)    # the dip's low
        assert setup["swing_high"] == pytest.approx(5.50)

    def test_earlier_than_the_break_entry(self):
        # 5.39 has not cleared the last candle's 5.40 high yet.
        assert detect_pullback(red_pullback(), price=5.39, cfg=CFG) is None
        assert detect_dip_green(red_pullback(), GREEN, 5.39, CFG) is not None

    def test_silent_while_the_candle_is_still_red(self):
        falling = bar(5.36, 5.37, 5.34, 5.35)
        assert detect_dip_green(red_pullback(), falling, 5.35, CFG) is None

    def test_silent_when_price_is_back_under_the_open(self):
        assert detect_dip_green(red_pullback(), GREEN, 5.35, CFG) is None

    def test_not_the_first_green_if_the_dip_already_turned(self):
        bars = red_pullback()
        bars[-1] = bar(5.34, 5.40, 5.33, 5.38)   # already green
        assert detect_dip_green(bars, GREEN, 5.39, CFG) is None

    def test_a_deeper_low_in_the_green_candle_is_the_stop(self):
        wick = bar(5.36, 5.40, 5.31, 5.39)
        setup = detect_dip_green(red_pullback(), wick, 5.39, CFG)
        assert setup["stop"] == pytest.approx(5.31)

    def test_rejects_a_dip_that_broke_down(self):
        bars = red_pullback()
        bars[-1] = bar(5.38, 5.40, 4.60, 4.70)   # -16% off the high
        assert detect_dip_green(bars, bar(4.70, 4.80, 4.65, 4.78),
                                4.78, CFG) is None

    def test_needs_a_swing_high_behind_it(self):
        rising = [bar(5.0, 5.1, 5.0, 5.1), bar(5.1, 5.3, 5.1, 5.3)]
        assert detect_dip_green(rising, bar(5.3, 5.5, 5.3, 5.5),
                                5.5, CFG) is None

    def test_needs_a_candle(self):
        assert detect_dip_green(red_pullback(), None, 5.39, CFG) is None
