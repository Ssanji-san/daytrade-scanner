"""Shared test fixture helpers."""


def make_state(**overrides):
    """A symbol state that qualifies for the HOD momentum scanner by default."""
    state = {
        "symbol": "TEST",
        "price": 3.00,
        "day_pct": 25.0,          # % up vs previous close
        "open_pct": 12.0,         # % gained since the 9:30 bell
        "day_volume": 2_000_000,
        "day_high": 3.03,
        "rvol": 8.0,
        "avg_volume": 400_000,
        "float_shares": 8_000_000,
        "has_news": True,
        "changes": {5: 3.0, 10: 6.0, 15: 9.0},
        "catalyst": {"category": "fda", "weight": 1.0, "score": 1.0,
                     "age_minutes": 12.0, "veto": False,
                     "headline": "TEST receives FDA approval"},
        "vwap": 2.85,
        "above_vwap": True,
        "setup": {"setup": "micro_pullback", "stop": 2.90,
                  "swing_high": 3.03, "pullback_low": 2.90, "trigger": 2.98},
    }
    state.update(overrides)
    return state
