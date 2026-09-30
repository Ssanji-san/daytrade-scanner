"""Scanner 2: small-cap high-of-day momentum.

Filters on Ross Cameron's five stock-selection criteria (float, % up
today, volume traded, relative volume, news) plus the price band and
proximity to the high of day. Stocks failing exactly one criterion go to
the dimmed "near" list so the user sees what's about to qualify.
"""
from . import catalyst
from .config import Config
from .countries import is_chinese


def _criteria(state, cfg: Config):
    """Ordered (name, passed) checks. Unknown values count as failures."""
    price, high = state["price"], state["day_high"]
    dist = 100.0 * (high - price) / high if high else None
    checks = [
        ("price", cfg.hod_min_price <= price <= cfg.hod_max_price),
        ("pct_up", (state["day_pct"] or 0) >= cfg.hod_min_pct_up),
        ("rvol", (state["rvol"] or 0) >= cfg.hod_min_rvol),
        ("float", state["float_shares"] is not None
                  and state["float_shares"] < cfg.hod_max_float),
        ("hod", dist is not None and dist <= cfg.hod_near_high_pct),
    ]
    if cfg.hod_min_open_pct and not state.get("premarket"):
        # Not "gapped up overnight" but "is being bought right now". A stock
        # that gapped 40% and has drifted sideways since the bell fails
        # this; one grinding up off the open passes.
        #
        # Only after the bell. It measures "% gained since 09:30", which is
        # not a weak number before 09:30 but an undefined one - there is no
        # open yet. Counting that as a failure blocked 31 of 33 pre-market
        # rows in the live journal, so no pre-market setup could ever
        # qualify. An open that is merely UNKNOWN after the bell still fails
        # (test_a_missing_open_counts_as_a_failure). Pre-market, "still
        # being bought" is carried by the hod check (within 6% of the high).
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
    if cfg.china_requires_news and is_chinese(state.get("country")) is not False:
        # Chinese small caps moving on nothing are pumps, so they need news
        # that is BREAKING, not merely recent: the bot already requires a
        # catalyst for everything, but a day-old headline still scores. An
        # unknown country is treated the same, as an unknown float fails.
        checks.append(("china_news", is_breaking(state.get("catalyst"), cfg)))
    return checks, dist


def is_breaking(found, cfg: Config):
    """A tradable catalyst no older than catalyst_fresh_minutes."""
    return (catalyst.is_tradable(found, cfg)
            and (found.get("age_minutes") or 0) <= cfg.catalyst_fresh_minutes)


def scan(states, cfg: Config):
    """Returns (qualified, near) row lists, both sorted by day % desc."""
    qualified, near = [], []
    # Two bands. The wider one decides what is looked at and graded; the
    # trading band is a criterion like any other, so a $7 mover lands in the
    # near list with "price" against its name and teaches the model
    # something, while never being buyable.
    ceiling = max(cfg.hod_observe_max_price or 0, cfg.hod_max_price)
    for state in states:
        if not (cfg.hod_min_price <= state["price"] <= ceiling):
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
