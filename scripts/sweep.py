"""Sweep the scanner gates against replayed history.

    python scripts/sweep.py --split 2026-08-01

**A grid search does not find "the best strategy."** It finds the thresholds
that best fit the data it searched. With a few hundred labeled alerts and six
knobs, some combination will look excellent by chance alone - that is
curve-fitting, and it is how backtested strategies die in production.

The only honest defence is a holdout. Everything before `--split` is used to
search; everything after is never touched until a winner has been chosen, and
then scored once. A combination that keeps its edge there is worth a second
look. One that collapses was noise, and the collapse is the useful result.

Two limits worth knowing:

* The search can only see rows the replay journalled. That is why the backtest
  captures a wider near-list than the live dashboard does.
* Win rate here means "reached +2R before -1R", the same label the model
  learns from. It is not a P&L simulation and does not model spread or fills.
"""
import argparse
import itertools
import json
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.config import DEFAULT                            # noqa: E402

# Deliberately coarse. A finer grid does not find a better answer on this much
# data, it just finds a luckier one.
GRID = {
    "rvol": [2.0, 3.0, 5.0, 8.0],
    "float_max": [20e6, 50e6, 200e6],
    "pct_up": [5.0, 10.0, 20.0],
    "dist_hod": [2.0, 4.0, 8.0],
    "catalyst": [0.0, 0.30, 0.60],
    "vwap": [True, False],
}


def load(path):
    """(day, features, label) for every graded alert."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    rows = []
    for r in db.execute("SELECT day, features, label FROM alerts "
                        "WHERE label IS NOT NULL"):
        try:
            rows.append((r["day"], json.loads(r["features"]), r["label"]))
        except ValueError:
            continue
    return rows


def passes(features, combo):
    """Would this row clear every gate in `combo`?"""
    if (features.get("rvol") or 0) < combo["rvol"]:
        return False
    float_shares = features.get("float_shares") or 0
    if not 0 < float_shares < combo["float_max"]:
        return False
    if (features.get("day_pct") or 0) < combo["pct_up"]:
        return False
    if (features.get("dist_from_hod") or 0) > combo["dist_hod"]:
        return False
    if (features.get("catalyst_score") or 0) < combo["catalyst"]:
        return False
    if combo["vwap"] and not features.get("above_vwap"):
        return False
    return True


def score(rows, combo):
    """(n, win_rate) for the rows a combo admits."""
    hits = [label for _, features, label in rows if passes(features, combo)]
    if not hits:
        return 0, None
    return len(hits), sum(hits) / len(hits)


def combos():
    keys = list(GRID)
    for values in itertools.product(*(GRID[k] for k in keys)):
        yield dict(zip(keys, values))


def describe(combo):
    return (f"rvol>={combo['rvol']:<4} float<{combo['float_max']/1e6:<5.0f}M "
            f"up>={combo['pct_up']:<4} hod<={combo['dist_hod']:<4} "
            f"cat>={combo['catalyst']:<4} vwap={combo['vwap']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", default=DEFAULT.backtest_journal_path)
    parser.add_argument("--split", required=True,
                        help="first day of the holdout (YYYY-MM-DD)")
    parser.add_argument("--min-trades", type=int, default=20,
                        help="ignore combos admitting fewer than this on train")
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()

    rows = load(args.journal)
    train = [r for r in rows if r[0] < args.split]
    holdout = [r for r in rows if r[0] >= args.split]
    if not train or not holdout:
        print(f"need data on both sides of {args.split}: "
              f"{len(train)} train, {len(holdout)} holdout")
        return

    base_train = sum(r[2] for r in train) / len(train)
    base_hold = sum(r[2] for r in holdout) / len(holdout)
    print(f"train    {len(train):>5} alerts, base rate {base_train:.0%}")
    print(f"holdout  {len(holdout):>5} alerts, base rate {base_hold:.0%}")
    print(f"searching {len(list(combos()))} combinations, "
          f"keeping those with >= {args.min_trades} on train")
    print()

    ranked = []
    for combo in combos():
        n, rate = score(train, combo)
        if rate is not None and n >= args.min_trades:
            ranked.append((rate, n, combo))
    if not ranked:
        print("nothing admitted enough rows - lower --min-trades")
        return
    ranked.sort(key=lambda t: (-t[0], -t[1]))

    print(f"{'':2} {'train':>14}   {'HOLDOUT':>14}   gates")
    print(f"{'':2} {'n    win':>14}   {'n    win':>14}")
    for i, (rate, n, combo) in enumerate(ranked[:args.top]):
        hn, hrate = score(holdout, combo)
        held = f"{hn:>4} {hrate:>6.0%}" if hrate is not None else f"{hn:>4}      -"
        print(f"{i + 1:>2} {n:>4} {rate:>6.0%}   {held}   {describe(combo)}")

    best_rate, best_n, best = ranked[0]
    hn, hrate = score(holdout, best)
    print()
    print("The honest read:")
    print(f"  best on train       {best_rate:.0%} over {best_n} alerts "
          f"(base {base_train:.0%}, lift {best_rate - base_train:+.1%})")
    if hrate is None:
        print("  on the holdout      admitted nothing - the edge was noise")
        return

    # A win rate measured over a few dozen alerts carries real sampling
    # noise, so quote the lift against it rather than treating any positive
    # number as an edge.
    stderr = math.sqrt(max(hrate * (1 - hrate), 1e-9) / hn)
    lift = hrate - base_hold
    print(f"  same gates held out {hrate:.0%} over {hn} alerts "
          f"(base {base_hold:.0%}, lift {lift:+.1%} +/- {stderr:.1%})")

    if lift <= stderr:
        print("  -> inside the noise. This is not an edge, it is a fit.")
    elif lift <= 2 * stderr:
        print("  -> positive but within two standard errors. Suggestive at "
              "best; do not trade it on this evidence.")
    else:
        print("  -> survived data it was never tuned on by more than two "
              "standard errors. Worth a closer look, still not proof.")

    # If the train ranking cannot pick the holdout winner, the ranking is
    # mostly noise - which is the most useful thing a sweep can tell you.
    scored_holdout = []
    for rate, n, combo in ranked[:args.top]:
        hn2, hrate2 = score(holdout, combo)
        if hrate2 is not None:
            scored_holdout.append((hrate2, hn2, rate, combo))
    if scored_holdout:
        best_out = max(scored_holdout, key=lambda t: (t[0], t[1]))
        if best_out[3] != best:
            print()
            print("  Note: the best combination on the holdout was NOT the "
                  "one the search picked.")
            print(f"        it scored {best_out[0]:.0%} over {best_out[1]} "
                  f"alerts while ranking {best_out[2]:.0%} on train:")
            print(f"        {describe(best_out[3])}")
            print("        A ranking that cannot pick its own winner is "
                  "fitting noise, not finding strategy.")


if __name__ == "__main__":
    main()
