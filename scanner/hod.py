"""Scanner 2: small-cap high-of-day momentum.

Filters on Ross Cameron's five stock-selection criteria (float, % up
today, volume traded, relative volume, news) plus the price band and
proximity to the high of day. Stocks failing more than the qualifying bar
go to the dimmed "near" list so the user sees what's about to qualify.

The bar is four of five, not five of five. In the video this strategy is
built on, Ross trades stocks meeting "at least four of the five" - failing
all five makes him cautious, not absent. Nine hard AND-gates is a stricter
reading than the source: over eight replayed months exactly two rows passed
all of them, while 1,225 failed exactly two.

Not every failure is equivalent, though, so the count has a floor.
DISQUALIFYING criteria are not demand pillars that a stock can be strong
enough elsewhere to overcome - they are the questions "may I buy this?"
and "can I get back out?", and a no there is final.
"""
from . import catalyst
from .config import Config

# `price` - out of the trading band. The observation band exists so $5-10
#   movers are graded for learning; letting one qualify would put real money
#   on a stock outside the risk model.
# `vwap`  - Ross names below-VWAP an entry disqualifier, not a weak pillar.
#   Below it the move is a fade.
# `liquidity` - not demand but exit risk. WVVIP, a preferred share printing
#   0-1,295 shares a DAY, showed a huge percentage move and an enormous
#   rvol against its near-zero baseline. Every demand pillar can look
#   perfect on something there is nobody on the other side of.
DISQUALIFYING = ("price", "vwap", "liquidity")


def _criteria(state, cfg: Config):
    """(checks, dist, disqualifying) for one row.

    `checks` is an ordered (name, passed) list - unknown values count as
    failures. `disqualifying` names the failures that can never be voted
    down by strength elsewhere.
    """
    price, high = state["price"], state["day_high"]
    dist = 100.0 * (high - price) / high if high else None
    hard = DISQUALIFYING
    checks = [
        ("price", cfg.hod_min_price <= price <= cfg.hod_max_price),
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
        reason = state.get("catalyst")
        checks.append(("news", catalyst.is_tradable(reason, cfg)))
        if reason and reason.get("veto"):
            # A missing catalyst is a pillar this stock does not have. An
            # offering is a reason to stay OUT, and no amount of demand
            # elsewhere outvotes it - so it joins the disqualifying set for
            # this row only. The name is unchanged: the dashboard shows the
            # user "news" either way.
            hard = hard + ("news",)
    return checks, dist, hard


def _qualifies(failed, hard, cfg: Config):
    """Four of five: a row may miss up to N demand pillars, but not one of
    the disqualifying checks, however strong it looks elsewhere."""
    if any(name in hard for name in failed):
        return False
    return len(failed) <= cfg.hod_max_failures_to_qualify


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
        checks, dist, hard = _criteria(state, cfg)
        failed = [name for name, ok in checks if not ok]
        if len(failed) > cfg.near_filter_max_failures:
            continue
        row = dict(state, failed=failed, dist_from_hod=dist)
        (qualified if _qualifies(failed, hard, cfg) else near).append(row)

    by_pct = lambda r: -(r["day_pct"] or 0)
    qualified.sort(key=by_pct)
    near.sort(key=by_pct)
    return qualified[:cfg.hod_rows], near[:cfg.hod_rows]
