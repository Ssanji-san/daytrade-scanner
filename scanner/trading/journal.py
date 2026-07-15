"""SQLite trade journal: alerts (with forward outcomes), trades, model versions.

The learning dataset comes from *alerts*, not just taken trades: every
HOD-qualified alert gets tracked until it either hits +2R (label 1), its
stop (label 0), or 30 minutes pass (label 0). Trades record what the bot
actually did, in R multiples, and survive restarts so the daily cap holds.
"""
import datetime as dt
import json
import pathlib
import sqlite3
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ALERT_WINDOW_SECONDS = 30 * 60
WIN_R = 2.0   # label 1 = reached +2R before -1R

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    ts INTEGER, day TEXT, symbol TEXT,
    price REAL, r_dollars REAL,
    features TEXT,
    mfe REAL DEFAULT 0, mae REAL DEFAULT 0,
    label INTEGER, resolved_ts INTEGER,
    UNIQUE(day, symbol)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    ts INTEGER, day TEXT, symbol TEXT,
    qty INTEGER, entry REAL, stop REAL, targets TEXT,
    features TEXT,
    exit_ts INTEGER, exit_price REAL, exit_reason TEXT,
    pnl REAL, r_multiple REAL
);
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY,
    ts INTEGER, samples INTEGER, holdout_acc REAL, weights TEXT
);
"""


def _day(ts):
    return dt.datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


class Journal:
    def __init__(self, path):
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---------------------------------------------------------- alerts

    def record_alert(self, ts, symbol, price, r_dollars, features):
        """One alert per symbol per trading day; returns id or None if dupe."""
        try:
            cur = self.db.execute(
                "INSERT INTO alerts (ts, day, symbol, price, r_dollars, features)"
                " VALUES (?,?,?,?,?,?)",
                (ts, _day(ts), symbol, price, r_dollars, json.dumps(features)))
            self.db.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def track_alert(self, alert_id, now_ts, price):
        """Update excursions; resolve the label when a threshold is crossed."""
        row = self.db.execute("SELECT * FROM alerts WHERE id=?",
                              (alert_id,)).fetchone()
        if row is None or row["label"] is not None:
            return
        move = price - row["price"]
        mfe, mae = max(row["mfe"], move), min(row["mae"], move)
        label = None
        if price <= row["price"] - row["r_dollars"]:
            label = 0
        elif price >= row["price"] + WIN_R * row["r_dollars"]:
            label = 1
        elif now_ts - row["ts"] > ALERT_WINDOW_SECONDS:
            label = 0
        self.db.execute(
            "UPDATE alerts SET mfe=?, mae=?, label=?, resolved_ts=? WHERE id=?",
            (mfe, mae, label, now_ts if label is not None else None, alert_id))
        self.db.commit()

    def open_alerts(self):
        rows = self.db.execute(
            "SELECT id, symbol FROM alerts WHERE label IS NULL").fetchall()
        return [(r["id"], r["symbol"]) for r in rows]

    def labeled_dataset(self):
        rows = self.db.execute(
            "SELECT features, label FROM alerts WHERE label IS NOT NULL"
            " ORDER BY ts").fetchall()
        return [(json.loads(r["features"]), r["label"]) for r in rows]

    # ---------------------------------------------------------- trades

    def record_trade_open(self, ts, symbol, qty, entry, stop, targets, features):
        cur = self.db.execute(
            "INSERT INTO trades (ts, day, symbol, qty, entry, stop, targets,"
            " features) VALUES (?,?,?,?,?,?,?,?)",
            (ts, _day(ts), symbol, qty, entry, stop, json.dumps(targets),
             json.dumps(features)))
        self.db.commit()
        return cur.lastrowid

    def record_trade_close(self, trade_id, ts, exit_price, exit_reason):
        row = self.db.execute("SELECT * FROM trades WHERE id=?",
                              (trade_id,)).fetchone()
        pnl = (exit_price - row["entry"]) * row["qty"]
        risk = row["entry"] - row["stop"]
        r_multiple = (exit_price - row["entry"]) / risk if risk else 0.0
        self.db.execute(
            "UPDATE trades SET exit_ts=?, exit_price=?, exit_reason=?,"
            " pnl=?, r_multiple=? WHERE id=?",
            (ts, exit_price, exit_reason, pnl, r_multiple, trade_id))
        self.db.commit()

    def trades_today(self, day):
        rows = self.db.execute(
            "SELECT * FROM trades WHERE day=? ORDER BY ts", (day,)).fetchall()
        return [dict(r) for r in rows]

    def day_pnl(self, day):
        row = self.db.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades WHERE day=?",
            (day,)).fetchone()
        return row["pnl"]

    def open_trade_rows(self):
        rows = self.db.execute(
            "SELECT * FROM trades WHERE exit_ts IS NULL ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    def recent_trades(self, limit=50):
        rows = self.db.execute(
            "SELECT * FROM trades WHERE exit_ts IS NOT NULL"
            " ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def rolling_stats(self, n=20):
        trades = self.recent_trades(n)
        if not trades:
            return {"count": 0, "win_rate": None, "expectancy_r": None}
        rs = [t["r_multiple"] for t in trades]
        wins = sum(1 for r in rs if r > 0)
        return {"count": len(rs), "win_rate": wins / len(rs),
                "expectancy_r": sum(rs) / len(rs)}

    # ---------------------------------------------------------- models

    def record_model(self, ts, samples, holdout_acc, weights):
        self.db.execute(
            "INSERT INTO models (ts, samples, holdout_acc, weights)"
            " VALUES (?,?,?,?)", (ts, samples, holdout_acc, json.dumps(weights)))
        self.db.commit()

    def latest_model(self):
        row = self.db.execute(
            "SELECT * FROM models ORDER BY ts DESC LIMIT 1").fetchone()
        if row is None:
            return None
        out = dict(row)
        out["weights"] = json.loads(out["weights"])
        return out

    def model_history(self, limit=20):
        rows = self.db.execute(
            "SELECT ts, samples, holdout_acc FROM models"
            " ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
