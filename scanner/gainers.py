"""Scanner 1: rolling top gainers over a 5/10/15-minute window."""
from .config import Config


def top_gainers(states, window, cfg: Config):
    """States with a positive `window`-minute change, biggest first."""
    rows = [s for s in states if (s["changes"].get(window) or 0) > 0]
    rows.sort(key=lambda s: -s["changes"][window])
    return rows[:cfg.gainer_rows]
