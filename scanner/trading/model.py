"""Alert scoring: tiny pure-Python logistic regression with a heuristic fallback.

The dataset is small (tens to hundreds of labeled alerts), so anything
fancier than standardized logistic regression would just memorize noise.
Below `min_samples` we don't pretend to have learned anything: a fixed,
transparent heuristic (relative volume + news catalyst) does the ranking.
"""
import math
import random

FEATURE_ORDER = ["rvol", "day_pct", "float_shares", "has_news",
                 "dist_from_hod", "change_5", "minutes_since_open",
                 "above_vwap", "catalyst_score", "catalyst_age"]


def _vector(features):
    return [float(features.get(name) or 0.0) for name in FEATURE_ORDER]


def _sigmoid(z):
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


class HeuristicScorer:
    """rvol does the ranking, a fresh headline adds a bump. Bounded [0, 1]."""

    def score(self, features):
        rvol = float(features.get("rvol") or 0.0)
        catalyst = float(features.get("catalyst_score") or 0.0)
        news = 1.0 if features.get("has_news") else 0.0
        return max(0.0, min(1.0, 0.35 + min(rvol, 20.0) / 50.0
                            + 0.25 * catalyst + 0.05 * news))


class LogisticScorer:
    def __init__(self, weights, bias, means, stds):
        self.weights, self.bias = weights, bias
        self.means, self.stds = means, stds

    def score(self, features):
        x = _vector(features)
        z = self.bias
        for i, value in enumerate(x):
            z += self.weights[i] * (value - self.means[i]) / self.stds[i]
        return _sigmoid(z)

    def to_weights(self):
        return {"weights": self.weights, "bias": self.bias,
                "means": self.means, "stds": self.stds,
                "features": FEATURE_ORDER}


def scorer_from_weights(weights):
    return LogisticScorer(weights["weights"], weights["bias"],
                          weights["means"], weights["stds"])


def _standardize(rows):
    n, dim = len(rows), len(FEATURE_ORDER)
    means = [sum(r[i] for r in rows) / n for i in range(dim)]
    stds = []
    for i in range(dim):
        var = sum((r[i] - means[i]) ** 2 for r in rows) / n
        stds.append(math.sqrt(var) or 1.0)
    return means, stds


def train(dataset, min_samples=40, seed=0, epochs=400, lr=0.1):
    """Returns (scorer, meta). meta['kind'] says which path was taken."""
    if len(dataset) < min_samples:
        return HeuristicScorer(), {"kind": "heuristic", "samples": len(dataset)}

    rng = random.Random(seed)
    shuffled = list(dataset)
    rng.shuffle(shuffled)
    split = max(1, len(shuffled) // 5)
    holdout, training = shuffled[:split], shuffled[split:]

    raw = [_vector(f) for f, _ in training]
    labels = [y for _, y in training]
    means, stds = _standardize(raw)
    rows = [[(v - means[i]) / stds[i] for i, v in enumerate(r)] for r in raw]

    dim = len(FEATURE_ORDER)
    weights, bias = [0.0] * dim, 0.0
    n = len(rows)
    for _ in range(epochs):
        grad_w, grad_b = [0.0] * dim, 0.0
        for row, y in zip(rows, labels):
            pred = _sigmoid(bias + sum(w * v for w, v in zip(weights, row)))
            err = pred - y
            grad_b += err
            for i, v in enumerate(row):
                grad_w[i] += err * v
        bias -= lr * grad_b / n
        for i in range(dim):
            weights[i] -= lr * grad_w[i] / n

    scorer = LogisticScorer(weights, bias, means, stds)
    correct = sum(1 for f, y in holdout if (scorer.score(f) >= 0.5) == bool(y))
    meta = {"kind": "logreg", "samples": len(dataset),
            "holdout_acc": correct / len(holdout),
            "weights": scorer.to_weights()}
    return scorer, meta
