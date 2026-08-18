"""Shared test fixture helpers."""


def make_state(**overrides):
    """A symbol state that qualifies for the HOD momentum scanner by default."""
    state = {
        "symbol": "TEST",
        "price": 5.50,
        "day_pct": 25.0,          # % up vs previous close
        "day_volume": 2_000_000,
        "day_high": 5.55,
        "rvol": 8.0,
        "float_shares": 8_000_000,
        "has_news": True,
        "changes": {5: 3.0, 10: 6.0, 15: 9.0},
        "catalyst": {"category": "fda", "weight": 1.0, "score": 1.0,
                     "age_minutes": 12.0, "veto": False,
                     "headline": "TEST receives FDA approval"},
        "vwap": 5.30,
        "above_vwap": True,
        "setup": {"setup": "micro_pullback", "stop": 5.35,
                  "swing_high": 5.55, "pullback_low": 5.35, "trigger": 5.48},
    }
    state.update(overrides)
    return state
