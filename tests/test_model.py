import random

from scanner.trading.model import scorer_from_weights, train


def alert(rvol=5.0, day_pct=15.0, float_shares=8e6, has_news=0.0,
          dist_from_hod=0.5, change_5=2.0, minutes_since_open=30.0):
    return {"rvol": rvol, "day_pct": day_pct, "float_shares": float_shares,
            "has_news": has_news, "dist_from_hod": dist_from_hod,
            "change_5": change_5, "minutes_since_open": minutes_since_open}


def planted_dataset(n=240, seed=7):
    """Winners are exactly the high-rvol alerts - a pattern to discover."""
    rng = random.Random(seed)
    data = []
    for _ in range(n):
        rvol = rng.uniform(1, 15)
        features = alert(rvol=rvol, day_pct=rng.uniform(10, 60),
                         has_news=float(rng.random() < 0.5),
                         minutes_since_open=rng.uniform(5, 120))
        data.append((features, 1 if rvol > 7 else 0))
    return data


class TestHeuristicFallback:
    def test_used_below_min_samples(self):
        scorer, meta = train(planted_dataset(10), min_samples=40)
        assert meta["kind"] == "heuristic"
        assert meta["samples"] == 10

    def test_heuristic_prefers_rvol_and_news(self):
        scorer, _ = train([], min_samples=40)
        low = scorer.score(alert(rvol=2.0, has_news=0.0))
        high = scorer.score(alert(rvol=12.0, has_news=0.0))
        with_news = scorer.score(alert(rvol=2.0, has_news=1.0))
        assert high > low
        assert with_news > low
        assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0


class TestLogisticRegression:
    def test_learns_planted_pattern(self):
        scorer, meta = train(planted_dataset(), min_samples=40, seed=3)
        assert meta["kind"] == "logreg"
        assert meta["holdout_acc"] >= 0.8
        assert scorer.score(alert(rvol=13.0)) > scorer.score(alert(rvol=2.0))

    def test_scores_bounded_and_missing_features_ok(self):
        scorer, _ = train(planted_dataset(), min_samples=40, seed=3)
        s = scorer.score({"rvol": 9.0})   # everything else missing
        assert 0.0 <= s <= 1.0

    def test_weights_roundtrip(self):
        scorer, meta = train(planted_dataset(), min_samples=40, seed=3)
        clone = scorer_from_weights(meta["weights"])
        probe = alert(rvol=11.0, has_news=1.0)
        assert abs(clone.score(probe) - scorer.score(probe)) < 1e-9


class TestSelfCalibratingThreshold:
    """A trained model sets its own bar from the scores it actually emits.

    The fixed 0.55 was tuned for the heuristic. A calibrated probability
    model scoring a population where ~1 in 4 setups wins sits far below
    that, so keeping the fixed bar silently stops all trading.
    """

    def dataset(self, n=60):
        rows = []
        for i in range(n):
            win = 1 if i % 4 == 0 else 0        # 25% base rate
            rows.append(({"rvol": 4.0 + win * 12, "day_pct": 10.0 + win * 20,
                          "float_shares": 8e6, "has_news": float(win),
                          "dist_from_hod": 0.5, "change_5": 2.0 + win,
                          "minutes_since_open": 30.0, "above_vwap": 1.0,
                          "catalyst_score": 0.3 + 0.6 * win,
                          "catalyst_age": 20.0}, win))
        return rows

    def test_trained_model_publishes_a_threshold(self):
        _, meta = train(self.dataset(), min_samples=40, percentile=75.0)
        assert meta["kind"] == "logreg"
        assert meta["threshold"] is not None

    def test_threshold_is_selective_but_still_admits_candidates(self):
        data = self.dataset()
        scorer, meta = train(data, min_samples=40, percentile=75.0)
        above = sum(1 for f, _ in data if scorer.score(f) >= meta["threshold"])
        assert 0 < above < len(data)            # a bar, not a wall

    def test_raising_the_percentile_raises_the_bar(self):
        data = self.dataset()
        _, loose = train(data, min_samples=40, percentile=50.0)
        _, strict = train(data, min_samples=40, percentile=90.0)
        assert strict["threshold"] >= loose["threshold"]

    def test_heuristic_keeps_the_fixed_bar(self):
        _, meta = train(self.dataset(n=10), min_samples=40)
        assert meta["kind"] == "heuristic"
        assert "threshold" not in meta
