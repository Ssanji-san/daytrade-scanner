"""SQLite trade journal: alerts (with forward outcomes), trades, model versions.

The learning dataset comes from *alerts*, not just taken trades: every
HOD-qualified alert gets tracked until it either reaches the target the bot
actually trades for (label 1), its stop (label 0), or the tracking window
expires (label 0). Trades record what the bot really did, in R multiples,
and survive restarts so the daily cap holds.

What counts as a win follows the strategy: a fixed number of cents in scalp
mode, WIN_R multiples of risk otherwise. See Journal.__init__.
"""
import datetime as dt
import json
import pathlib
import sqlite3
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ALERT_WINDOW_SECONDS = 30 * 60      # default; Config.bot_alert_window_minutes wins
WIN_R = 2.0   # label 1 = reached +2R before -1R, when no cent target is set

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    ts INTEGER, day TEXT, symbol TEXT,
    price REAL, r_dollars REAL,
    features TEXT,
    mfe REAL DEFAULT 0, mae REAL DEFAULT 0,
    label INTEGER, resolved_ts INTEGER, resolved_r REAL,
    observed INTEGER DEFAULT 0,
    decision TEXT, failed TEXT,
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


def _join_failed(failed):
    """Criteria names in one field, the way `decision` joins its reasons.

    An empty list is not the same as unknown: "" means the row passed every
    criterion, NULL means nothing recorded it.
    """
    if failed is None:
        return None
    return "+".join(failed)


def _day(ts):
    return dt.datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")


class Journal:
    # SQLite reports a connection whose file was replaced as
    # "attempt to write a readonly database" (SQLITE_READONLY_DBMOVED).
    # The cloud workflow commits cache/journal.db every 10 minutes while
    # the bot is running, and git rewrites the file underneath us, so the
    # connection has to be able to heal itself or the bot goes deaf for
    # the rest of the session.
    RECOVERABLE = ("readonly", "moved", "disk i/o", "closed database",
                   "database is locked", "no such table")

    def __init__(self, path, alert_window_minutes=None, win_target_cents=None):
        # The grading horizon has to match how long the bot actually holds a
        # trade. Labelling a loss at 30 minutes while the time stop runs for
        # four hours does not measure the strategy, it measures the timer:
        # 186 of 414 replayed losses were the 30-minute clock expiring, not
        # the stop being hit.
        self.alert_window_seconds = (ALERT_WINDOW_SECONDS
                                     if alert_window_minutes is None
                                     else int(alert_window_minutes) * 60)
        # And the target has to match what the bot actually takes. Grading on
        # +2R while the bot banks at +20c measured a move it never waits for:
        # 2R on the flat 5% stop is a +10% move, and 20c is 1.3R on a $3
        # stock and 0.8R on a $5 one. None keeps the R rule for the swing
        # path, which really does hold out for a multiple of risk.
        self.win_target_cents = win_target_cents
        self.path = str(path)
        pathlib.Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connect()

    def _connect(self):
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        for table in ("alerts", "trades"):      # migrate older journals
            try:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN setup TEXT")
            except sqlite3.OperationalError:
                pass
        try:
            self._db.execute(
                "ALTER TABLE alerts ADD COLUMN observed INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            self._db.execute("ALTER TABLE alerts ADD COLUMN resolved_r REAL")
        except sqlite3.OperationalError:
            pass
        try:
            self._db.execute("ALTER TABLE alerts ADD COLUMN decision TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            self._db.execute("ALTER TABLE alerts ADD COLUMN failed TEXT")
        except sqlite3.OperationalError:
            pass
        self._db.commit()

    def _recoverable(self, exc):
        return any(hint in str(exc).lower() for hint in self.RECOVERABLE)

    def _execute(self, sql, params=()):
        """Run a statement, reopening once if the file was swapped."""
        try:
            return self._db.execute(sql, params)
        except (sqlite3.OperationalError, sqlite3.ProgrammingError) as exc:
            if not self._recoverable(exc):
                raise
            self._connect()
            return self._db.execute(sql, params)

    def _commit(self):
        try:
            return self._db.commit()
        except (sqlite3.OperationalError, sqlite3.ProgrammingError) as exc:
            if not self._recoverable(exc):
                raise
            self._connect()
            return self._db.commit()

    # ---------------------------------------------------------- alerts

    def record_alert(self, ts, symbol, price, r_dollars, features, setup=None,
                     observed=0, failed=None):
        """One alert per symbol per trading day; returns id, or None if dupe.

        `observed` marks a near-miss: graded for learning, never traded.
        `failed` is the criteria it missed, narrowing across the session -
        see _narrow_failed.

        A symbol nearly always appears in the near list before it qualifies,
        and the row is UNIQUE(day, symbol) - so the row written for the day
        was the near miss, and the later qualifying row was dropped on the
        floor. Every alert in the journal ended up marked observed=1 while
        real trades were being taken, which is the wrong distribution to
        train on. A qualifying alert therefore UPGRADES the near-miss row it
        finds. The upgrade is one-way, and the price and features stay as
        first recorded so the alert still measures the moment it was spotted.
        """
        row = self._execute(
            "SELECT id, observed, failed FROM alerts WHERE day=? AND symbol=?",
            (_day(ts), symbol)).fetchone()
        if row is not None:
            self._narrow_failed(row, failed)
            if row["observed"] and not int(observed):
                self._execute(
                    "UPDATE alerts SET observed=0, setup=COALESCE(?, setup)"
                    " WHERE id=?", (setup, row["id"]))
                self._commit()
                return row["id"]
            return None
        try:
            cur = self._execute(
                "INSERT INTO alerts (ts, day, symbol, price, r_dollars,"
                " features, setup, observed, failed)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, _day(ts), symbol, price, r_dollars, json.dumps(features),
                 setup, int(observed), _join_failed(failed)))
            self._commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None          # raced with another writer; it is recorded

    def _narrow_failed(self, row, failed):
        """Keep the CLOSEST the row ever came to qualifying.

        A symbol is graded every cycle and its failures move with the price:
        two missing pillars at 09:35, one at 10:15 as volume builds. The
        interesting number is the smallest - "it got within one criterion,
        and that criterion was float" - so this only ever narrows. Same rule
        as `decision`, which only ever upgrades.
        """
        if failed is None:
            return
        joined = _join_failed(failed)
        current = row["failed"]
        if current is not None and len(current.split("+")) <= len(failed):
            return
        self._execute("UPDATE alerts SET failed=? WHERE id=?",
                      (joined, row["id"]))
        self._commit()

    # A qualifying alert is journalled and tracked to its outcome whether or
    # not the bot buys it, so the journal already knows what every setup went
    # on to do. What it did not know was what the BOT did about it: the
    # rejection reasons should_enter computes were thrown away at the call
    # site. Recording them turns the alert table into the question worth
    # asking - of the setups it declined, which ones paid, and what rule
    # stopped it.
    DECISION_RANK = {None: 0, "no_setup": 0}

    def _decision_rank(self, decision):
        """How informative a decision is. Only ever upgrade.

        A symbol is looked at every cycle, so its decision changes through
        the session: no setup at 10:00, a setup declined on score at 10:15,
        maybe bought at 10:30. Last-write-wins would let a later cycle with
        no entry trigger erase the fact that a real setup was turned down,
        which is the one thing worth keeping. So the ladder only goes up,
        the same way `observed` only ever upgrades to tradable.
        """
        if decision == "taken":
            return 2
        return self.DECISION_RANK.get(decision, 1)

    def record_decision(self, ts, symbol, decision):
        """What the bot did about today's alert for `symbol`.

        "taken", "no_setup", or the reason it passed - should_enter's own
        vocabulary ("score", "daily_cap", "concurrency", "loss_cap",
        "already_traded", "window", "kill_switch"), joined with "+" when
        more than one applied.
        """
        row = self._execute(
            "SELECT id, decision FROM alerts WHERE day=? AND symbol=?",
            (_day(ts), symbol)).fetchone()
        if row is None:
            return False
        if self._decision_rank(decision) < self._decision_rank(row["decision"]):
            return False
        if row["decision"] == decision:
            return False
        self._execute("UPDATE alerts SET decision=? WHERE id=?",
                      (decision, row["id"]))
        self._commit()
        return True

    def miss_reasons(self, day=None, since_ts=None):
        """Per criterion: how many rows it blocked, and what they did next.

        The question the near list could never answer. `blocked_alone`
        counts the rows where this was the ONLY thing in the way - those
        are the ones a change to that criterion would actually buy, and
        `alone_wins` says how many of them went on to reach the target.
        A criterion that blocks a lot and wins nothing is doing its job.
        """
        sql = ("SELECT failed, label FROM alerts"
               " WHERE failed IS NOT NULL AND failed != ''")
        params = []
        if day:
            sql += " AND day=?"
            params.append(day)
        if since_ts:
            sql += " AND ts>=?"
            params.append(since_ts)

        tally = {}
        for row in self._execute(sql, tuple(params)).fetchall():
            names = row["failed"].split("+")
            for name in names:
                seen = tally.setdefault(name, {"criterion": name, "blocked": 0,
                                               "blocked_alone": 0,
                                               "alone_wins": 0,
                                               "alone_resolved": 0})
                seen["blocked"] += 1
                if len(names) == 1:
                    seen["blocked_alone"] += 1
                    if row["label"] is not None:
                        seen["alone_resolved"] += 1
                        seen["alone_wins"] += int(row["label"])
        return sorted(tally.values(), key=lambda r: -r["blocked"])

    def decision_report(self, day=None, since_ts=None):
        """Per decision: how many resolved, how many won, mean R.

        Only resolved, non-observed alerts count - an alert still tracking
        has no outcome yet, and a near miss was never the bot's to take.
        """
        sql = ("SELECT COALESCE(decision, 'no_setup') AS decision,"
               " COUNT(*) AS n, SUM(label) AS wins,"
               " AVG(resolved_r) AS mean_r, MAX(mfe) AS best_mfe"
               " FROM alerts WHERE observed=0 AND label IS NOT NULL")
        params = []
        if day:
            sql += " AND day=?"
            params.append(day)
        if since_ts:
            sql += " AND ts>=?"
            params.append(since_ts)
        sql += " GROUP BY 1 ORDER BY n DESC"
        return [dict(r) for r in self._execute(sql, tuple(params)).fetchall()]

    def missed_winners(self, limit=50, since_ts=None):
        """Alerts that hit the target after the bot declined them.

        The costliest rows in the journal: the setup was real, the outcome
        was a win, and a rule the bot owns is why it was not taken.
        """
        sql = ("SELECT day, symbol, ts, price, decision, resolved_r, mfe"
               " FROM alerts WHERE observed=0 AND label=1"
               " AND decision IS NOT NULL AND decision NOT IN ('taken')")
        params = []
        if since_ts:
            sql += " AND ts>=?"
            params.append(since_ts)
        sql += " ORDER BY resolved_r DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._execute(sql, tuple(params)).fetchall()]

    def track_alert(self, alert_id, now_ts, price, high=None, low=None):
        """Update excursions; resolve the label when a threshold is crossed.

        High/low come from the real 1-minute bar. Grading on polled last
        prices alone silently ignores the wick that would have stopped the
        trade out, which teaches the model a rosier world than it trades in.
        """
        row = self._execute("SELECT * FROM alerts WHERE id=?",
                              (alert_id,)).fetchone()
        if row is None:
            return
        high = max(high or price, price)
        low = min(low or price, price)
        mfe = max(row["mfe"], high - row["price"])
        mae = min(row["mae"], low - row["price"])
        label, resolved = row["label"], row["resolved_ts"]
        resolved_r = row["resolved_r"]
        # Excursions keep recording after the label is set. Stopping there
        # froze every winner's mfe at the minute it crossed +2R, so a trade
        # that went on to run 10R was filed as exactly 2R and the model had
        # no way to tell a scratch from a monster. The label never changes
        # once decided.
        if label is None:
            # resolved_r is what the position was actually worth when it
            # ended, in R. A stop-out exits at the stop and a target exits
            # at the target, but a timeout exits at whatever the market is
            # then - and assuming that is break-even was quietly deciding
            # the answer, since timeouts are the large majority of outcomes.
            win_at, win_r = self._win_level(row)
            if low <= row["price"] - row["r_dollars"]:
                label, resolved_r = 0, -1.0        # the wick took the stop
            elif high >= win_at:
                label, resolved_r = 1, win_r
            elif now_ts - row["ts"] > self.alert_window_seconds:
                label = 0                          # the clock, not the stop
                resolved_r = ((price - row["price"]) / row["r_dollars"]
                              if row["r_dollars"] else 0.0)
            if label is not None:
                resolved = now_ts
        self._execute(
            "UPDATE alerts SET mfe=?, mae=?, label=?, resolved_ts=?,"
            " resolved_r=? WHERE id=?",
            (mfe, mae, label, resolved, resolved_r, alert_id))
        self._commit()

    def _win_level(self, row):
        """(price that counts as a win, what it is worth in R).

        The R value is not a constant under a cent target: 20c is 4R on a $1
        stock and 0.8R on a $5 one, and that difference is the strategy's
        whole character, so it is recorded rather than assumed.
        """
        if self.win_target_cents:
            r_dollars = row["r_dollars"]
            return (row["price"] + self.win_target_cents,
                    self.win_target_cents / r_dollars if r_dollars else WIN_R)
        return row["price"] + WIN_R * row["r_dollars"], WIN_R

    def tracking_alerts(self, day, now_ts):
        """Alerts still worth marking today - labelled ones included.

        A resolved alert keeps getting marked until it falls out of the
        tracking horizon, so mfe records how far a winner really ran.
        """
        rows = self._execute(
            "SELECT id, symbol FROM alerts WHERE day=? AND ts > ?",
            (day, now_ts - self.alert_window_seconds)).fetchall()
        return [(r["id"], r["symbol"]) for r in rows]

    def recent_alerts(self, limit=40):
        """Newest alerts with their graded outcome, for the dashboard."""
        rows = self._execute(
            "SELECT ts, day, symbol, price, setup, label, mfe, mae, observed"
            " FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def setup_stats(self):
        """Win rate and expectancy per setup type - which patterns work."""
        rows = self._execute(
            "SELECT COALESCE(setup,'unknown') AS setup, COUNT(*) n,"
            " AVG(r_multiple) exp_r,"
            " SUM(CASE WHEN r_multiple > 0 THEN 1 ELSE 0 END) wins"
            " FROM trades WHERE exit_ts IS NOT NULL GROUP BY 1").fetchall()
        return [dict(r) for r in rows]

    def learning_progress(self, min_samples):
        """How close the model is to training, split by data source."""
        row = self._execute(
            "SELECT COUNT(*) n,"
            " SUM(CASE WHEN observed=0 THEN 1 ELSE 0 END) tradable"
            " FROM alerts WHERE label IS NOT NULL").fetchone()
        return {"labeled": row["n"] or 0,
                "tradable": row["tradable"] or 0,
                "needed": min_samples}

    def outcome_rows(self):
        """Graded alerts with their excursions - what a report needs.

        `label` says whether the target came before the stop; `mfe`/`mae` how far it
        actually went, which is what separates a scratch from a runner and a
        real stop-out from the clock expiring.
        """
        rows = self._execute(
            "SELECT day, symbol, setup, price, r_dollars, mfe, mae, label,"
            " observed, resolved_r FROM alerts WHERE label IS NOT NULL"
            " ORDER BY ts"
        ).fetchall()
        return [dict(r) for r in rows]

    def labeled_dataset(self):
        rows = self._execute(
            "SELECT features, label FROM alerts WHERE label IS NOT NULL"
            " ORDER BY ts").fetchall()
        return [(json.loads(r["features"]), r["label"]) for r in rows]

    # ---------------------------------------------------------- trades

    def record_trade_open(self, ts, symbol, qty, entry, stop, targets, features,
                          setup=None):
        cur = self._execute(
            "INSERT INTO trades (ts, day, symbol, qty, entry, stop, targets,"
            " features, setup) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, _day(ts), symbol, qty, entry, stop, json.dumps(targets),
             json.dumps(features), setup))
        self._commit()
        return cur.lastrowid

    def update_trade_entry(self, trade_id, entry):
        """Correct the entry to what the order actually filled at.

        record_trade_open stores the signal price, because that is all that
        is known when the order is submitted. The R multiple has to be
        measured against what the fill really cost.
        """
        self._execute("UPDATE trades SET entry=? WHERE id=?", (entry, trade_id))
        self._commit()

    def delete_trade(self, trade_id):
        """Drop a trade that never happened - an entry that never filled."""
        self._execute("DELETE FROM trades WHERE id=?", (trade_id,))
        self._commit()

    def record_trade_close(self, trade_id, ts, exit_price, exit_reason):
        row = self._execute("SELECT * FROM trades WHERE id=?",
                              (trade_id,)).fetchone()
        pnl = (exit_price - row["entry"]) * row["qty"]
        risk = row["entry"] - row["stop"]
        r_multiple = (exit_price - row["entry"]) / risk if risk else 0.0
        self._execute(
            "UPDATE trades SET exit_ts=?, exit_price=?, exit_reason=?,"
            " pnl=?, r_multiple=? WHERE id=?",
            (ts, exit_price, exit_reason, pnl, r_multiple, trade_id))
        self._commit()

    def all_trades(self):
        """Every trade ever recorded, for reporting over a whole backtest."""
        rows = self._execute("SELECT * FROM trades ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    def trades_today(self, day):
        rows = self._execute(
            "SELECT * FROM trades WHERE day=? ORDER BY ts", (day,)).fetchall()
        return [dict(r) for r in rows]

    def losses_today(self, day):
        """Closed losing trades today - the day's kill switch counts these."""
        row = self._execute(
            "SELECT COUNT(*) n FROM trades WHERE day=? AND exit_ts IS NOT NULL"
            " AND r_multiple < 0", (day,)).fetchone()
        return row["n"] or 0

    def day_pnl(self, day):
        row = self._execute(
            "SELECT COALESCE(SUM(pnl), 0) AS pnl FROM trades WHERE day=?",
            (day,)).fetchone()
        return row["pnl"]

    def open_trade_rows(self):
        rows = self._execute(
            "SELECT * FROM trades WHERE exit_ts IS NULL ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    def recent_trades(self, limit=50):
        rows = self._execute(
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
        self._execute(
            "INSERT INTO models (ts, samples, holdout_acc, weights)"
            " VALUES (?,?,?,?)", (ts, samples, holdout_acc, json.dumps(weights)))
        self._commit()

    def latest_model(self):
        row = self._execute(
            "SELECT * FROM models ORDER BY ts DESC LIMIT 1").fetchone()
        if row is None:
            return None
        out = dict(row)
        out["weights"] = json.loads(out["weights"])
        return out

    def model_history(self, limit=20):
        rows = self._execute(
            "SELECT ts, samples, holdout_acc FROM models"
            " ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
