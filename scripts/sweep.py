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
* Scoring is **expectancy in R**, not win rate. Under a 20% stop roughly
  85% of alerts end on the time stop rather than at a target, so win rate
  measures the rare tail while the money is in the scratch distribution. A
  gate set that raises win rate and lowers expectancy is worse, not better.
* It is not a P&L simulation. **Spread and slippage are not modelled**, and
  they are the same order of magnitude as the edge being measured - so a
  positive result here is a reason to look closer, never a reason to trade.
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
# Reuse the replay's own outcome logic rather than restating it: two
# definitions of "what was this trade worth" would drift apart silently.
from scripts.backtest import outcome, timeout_r               # noqa: E402

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


def row_r(row):
    """What this alert was worth in R under the bot's actual exit policy.

    Half banked at +2R with the rest riding, a stop-out losing 1R, and a
    timeout worth whatever it was actually closed at.
    """
    kind = outcome(row)
    if kind == "stopped":
        return -1.0
    if kind == "timeout":
        return timeout_r(row)
    ran = (row["mfe"] / row["r_dollars"]) if row["r_dollars"] else 2.0
    return 1.0 + 0.5 * max(2.0, ran - 1.0)


def load(path):
    """(day, features, R) for every graded alert."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    rows = []
    for r in db.execute("SELECT day, features, label, mfe, mae, r_dollars,"
                        " resolved_r FROM alerts WHERE label IS NOT NULL"):
        try:
            features = json.loads(r["features"])
        except ValueError:
            continue
        rows.append((r["day"], features, row_r(dict(r))))
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
    """(n, mean R) for the rows a combo admits."""
    taken = [r for _, features, r in rows if passes(features, combo)]
    if not taken:
        return 0, None
    return len(taken), sum(taken) / len(taken)


def stderr_of(rows, combo):
    """Standard error of the mean R - how much of this is sampling noise.

    R multiples are far more skewed than a win rate (one runner can carry a
    month), so the spread has to be measured rather than derived from a
    proportion the way the old win-rate version did.
    """
    taken = [r for _, features, r in rows if passes(features, combo)]
    n = len(taken)
    if n < 2:
        return None
    mean = sum(taken) / n
    var = sum((r - mean) ** 2 for r in taken) / (n - 1)
    return math.sqrt(var / n)


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
    parser.add_argument("--min-trades", type=int, default=50,
                        help="ignore combos admitting fewer than this")
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
    print(f"train    {len(train):>6} alerts, base {base_train:+.3f}R")
    print(f"holdout  {len(holdout):>6} alerts, base {base_hold:+.3f}R")
    print(f"searching {len(list(combos()))} combinations, keeping those "
          f"with >= {args.min_trades} on train")
    print()

    ranked = []
    for combo in combos():
        n, exp_r = score(train, combo)
        if exp_r is not None and n >= args.min_trades:
            ranked.append((exp_r, n, combo))
    if not ranked:
        print("nothing admitted enough rows - lower --min-trades")
        return
    ranked.sort(key=lambda t: (-t[0], -t[1]))

    print(f"{'':2} {'train':>15}   {'HOLDOUT':>15}   gates")
    print(f"{'':2} {'n       R':>15}   {'n       R':>15}")
    for i, (exp_r, n, combo) in enumerate(ranked[:args.top]):
        hn, hexp = score(holdout, combo)
        held = f"{hn:>5} {hexp:>+8.3f}" if hexp is not None else f"{hn:>5}        -"
        print(f"{i + 1:>2} {n:>5} {exp_r:>+8.3f}   {held}   {describe(combo)}")

    best_exp, best_n, best = ranked[0]
    hn, hexp = score(holdout, best)
    print()
    print("The honest read:")
    print(f"  best on train       {best_exp:+.3f}R over {best_n} alerts "
          f"(base {base_train:+.3f}R)")
    if hexp is None:
        print("  on the holdout      admitted nothing - the edge was noise")
        return

    stderr = stderr_of(holdout, best)
    print(f"  same gates held out {hexp:+.3f}R over {hn} alerts "
          f"(base {base_hold:+.3f}R, +/- {stderr:.3f})")

    # Pre-registered before the numbers were seen: positive on data it was
    # never tuned on, by more than two standard errors, over at least 50
    # alerts. Anything short of that is a fit, and gets reported as one.
    if hn < args.min_trades:
        print(f"  -> only {hn} holdout alerts. Too few to judge.")
    elif hexp <= 0:
        print("  -> negative out of sample. The edge did not survive.")
    elif hexp <= 2 * stderr:
        print(f"  -> positive but within two standard errors "
              f"({2 * stderr:.3f}). Not distinguishable from noise.")
    else:
        print("  -> positive by more than two standard errors on data it "
              "was never tuned on.")
        print("     Still not proof: spread and slippage are unmodelled and "
              "are the same")
        print(f"     size as this edge - {hexp * 100 / 5:.1f}% of notional at "
              "a 20% stop.")

    # If the train ranking cannot pick the holdout winner, the ranking is
    # mostly noise - which is the most useful thing a sweep can tell you.
    scored_holdout = []
    for exp_r, n, combo in ranked[:args.top]:
        hn2, hexp2 = score(holdout, combo)
        if hexp2 is not None:
            scored_holdout.append((hexp2, hn2, exp_r, combo))
    if scored_holdout:
        best_out = max(scored_holdout, key=lambda t: (t[0], t[1]))
        if best_out[3] != best:
            print()
            print("  Note: the best combination on the holdout was NOT the "
                  "one the search picked.")
            print(f"        it scored {best_out[0]:+.3f}R over {best_out[1]} "
                  f"alerts while ranking {best_out[2]:+.3f}R on train:")
            print(f"        {describe(best_out[3])}")
            print("        A ranking that cannot pick its own winner is "
                  "fitting noise, not finding strategy.")


if __name__ == "__main__":
    main()
