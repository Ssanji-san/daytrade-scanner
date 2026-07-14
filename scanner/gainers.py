"""Scanner 1: rolling top gainers over a 5/10/15-minute window."""
from .config import Config


def _change(state, window):
    changes = state["changes"]
    # int keys in-process, string keys once JSON-serialized
    return changes.get(window, changes.get(str(window)))


def top_gainers(states, window, cfg: Config):
    """States with a positive `window`-minute change, biggest first."""
    rows = [s for s in states if (_change(s, window) or 0) > 0]
    rows.sort(key=lambda s: -_change(s, window))
    return rows[:cfg.gainer_rows]
