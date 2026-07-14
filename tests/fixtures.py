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
    }
    state.update(overrides)
    return state
