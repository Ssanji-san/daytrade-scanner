"""Catalyst quality: is the news a *reason* to run, or just a headline?

A headline is not a catalyst. An FDA approval or a buyout moves a small cap
5-20R; a routine press release does not; and a share offering actively
kills the move it looks like it caused. The scanners already require *some*
news - this scores the reason behind it, because the size of the reason is
what separates a 1R scalp from a runner.

Two things drive the score:
  weight     - what kind of news it is (approval vs partnership vs dilution)
  freshness  - news breaking now runs; yesterday's news is already priced in

Deliberately plain keyword matching: no model, no I/O, and you can always
read off exactly why a symbol scored what it did.
"""
import re

# (category, weight, pattern). First match wins, so dilution is tested
# first - an offering headline is usually dressed in upbeat words.
CATEGORIES = [
    ("offering", 0.05,
     r"offering|dilut|registered direct|warrant|shelf|atm program|"
     r"\bs-1\b|\bs-3\b|pricing of|public offer"),
    ("fda", 1.00,
     r"\bfda\b|approval|approved|clearance|breakthrough|orphan drug|"
     r"phase [23]|pivotal|topline"),
    ("merger", 1.00,
     r"acquisit|acquire|merger|buyout|takeover|tender offer|"
     r"definitive agreement|to be acquired"),
    ("contract", 0.80,
     r"contract|awarded|\baward\b|purchase order|selected by|wins\b"),
    ("earnings", 0.80,
     r"earnings|beats|record revenue|raises guidance|profitab|"
     r"q[1-4] results|revenue (up|jump|surge)"),
    ("uplisting", 0.65,
     r"uplist|nasdaq listing|nyse listing|begins trading"),
    ("hype", 0.60,
     r"\bai\b|artificial intelligence|quantum|crypto|bitcoin|blockchain|"
     r"robotic|space|\bev\b|nuclear"),
    ("partnership", 0.55,
     r"partnership|collaborat|joint venture|agreement with|licensing"),
]
OTHER = ("other", 0.35)


def classify(headline):
    """(category, weight) for one headline."""
    text = (headline or "").lower()
    for category, weight, pattern in CATEGORIES:
        if re.search(pattern, text):
            return category, weight
    return OTHER


def freshness(age_minutes, cfg):
    """1.0 while the news is breaking, decaying as it gets priced in."""
    if age_minutes <= cfg.catalyst_fresh_minutes:
        return 1.0
    window = cfg.news_max_age_hours * 60 - cfg.catalyst_fresh_minutes
    if window <= 0:
        return 1.0
    over = (age_minutes - cfg.catalyst_fresh_minutes) / window
    return max(0.25, 1.0 - 0.75 * min(1.0, over))


def score_news(items, now_ts, cfg):
    """Best catalyst among a symbol's headlines, or None if it has none.

    Returns {category, weight, score, age_minutes, headline, veto}. A
    dilution headline anywhere in the window sets veto and floors the
    score: an offering is a reason to stay out, however good the rest
    of the news looks.
    """
    best, veto = None, False
    for item in items or []:
        age_minutes = (now_ts - item.get("ts", 0)) / 60.0
        if age_minutes < -5 or age_minutes > cfg.news_max_age_hours * 60:
            continue
        category, weight = classify(item.get("headline", ""))
        if category in cfg.catalyst_veto:
            veto = True
        candidate = {
            "category": category,
            "weight": weight,
            "age_minutes": round(age_minutes, 1),
            "headline": item.get("headline", ""),
            "score": round(weight * freshness(age_minutes, cfg), 3),
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    if best is None:
        return None
    best["veto"] = veto
    if veto:
        best["score"] = min(best["score"], 0.10)
    return best


def is_tradable(catalyst, cfg):
    """A real reason, recent enough, and not a dilution event."""
    if not catalyst or catalyst.get("veto"):
        return False
    return catalyst["score"] >= cfg.catalyst_min_score
