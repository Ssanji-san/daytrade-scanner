"""Scanner 2: small-cap high-of-day momentum.

Filters on Ross Cameron's five stock-selection criteria (float, % up
today, volume traded, relative volume, news) plus the price band and
proximity to the high of day. Stocks failing exactly one criterion go to
the dimmed "near" list so the user sees what's about to qualify.
"""
from . import catalyst
from .config import Config


def _criteria(state, cfg: Config):
    """Ordered (name, passed) checks. Unknown values count as failures."""
    price, high = state["price"], state["day_high"]
    dist = 100.0 * (high - price) / high if high else None
    checks = [
        ("pct_up", (state["day_pct"] or 0) >= cfg.hod_min_pct_up),
        ("rvol", (state["rvol"] or 0) >= cfg.hod_min_rvol),
        ("float", state["float_shares"] is not None
                  and state["float_shares"] < cfg.hod_max_float),
        ("hod", dist is not None and dist <= cfg.hod_near_high_pct),
    ]
    if cfg.hod_min_open_pct:
        # Not "gapped up overnight" but "is being bought right now". A stock
        # that gapped 40% and has drifted sideways since the bell fails
        # this; one grinding up off the open passes.
        checks.append(("open_drive",
                       (state.get("open_pct") or 0) >= cfg.hod_min_open_pct))
    if cfg.hod_min_avg_volume:
        checks.append(("liquidity",
                       (state.get("avg_volume") or 0) >= cfg.hod_min_avg_volume))
    if cfg.hod_min_volume:
        checks.insert(1, ("volume",
                          (state["day_volume"] or 0) >= cfg.hod_min_volume))
    if cfg.require_vwap:
        # Long only above VWAP - below it the move is a fade, not a trend.
        checks.append(("vwap", bool(state.get("above_vwap"))))
    if cfg.hod_require_news:
        # Not "is there a headline" but "is there a *reason*": a real
        # catalyst, still fresh, and no share offering behind the move.
        checks.append(("news", catalyst.is_tradable(state.get("catalyst"),
                                                    cfg)))
    return checks, dist


def scan(states, cfg: Config):
    """Returns (qualified, near) row lists, both sorted by day % desc."""
    qualified, near = [], []
    for state in states:
        if not (cfg.hod_min_price <= state["price"] <= cfg.hod_max_price):
            continue
        checks, dist = _criteria(state, cfg)
        failed = [name for name, ok in checks if not ok]
        if len(failed) > cfg.near_filter_max_failures:
            continue
        row = dict(state, failed=failed, dist_from_hod=dist)
        (qualified if not failed else near).append(row)

    by_pct = lambda r: -(r["day_pct"] or 0)
    qualified.sort(key=by_pct)
    near.sort(key=by_pct)
    return qualified[:cfg.hod_rows], near[:cfg.hod_rows]
