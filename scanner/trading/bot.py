"""The trading bot loop: journal alerts, pick entries, manage exits.

Decision logic lives in strategy.py/model.py (tested); this module is the
orchestration around them plus two pure, tested helpers
(features_from_row, choose_entries). Paper account only — Broker enforces it.
"""
import asyncio
import datetime as dt
import traceback

import aiohttp

from ..config import Config
from .broker import Broker
from .journal import Journal
from .model import train
from .strategy import (ET, bankroll_from, buying_power, exit_levels,
                       is_doji, position_slots, runner_trail_pct,
                       bank_split, should_enter, size_position,
                       split_qty, technical_stop, weighted_exit,
                       _parse_hhmm)

MARKET_OPEN = dt.time(9, 30)

# An entry order in one of these states bought nothing and never will.
DEAD_ORDER_STATES = ("canceled", "cancelled", "expired", "rejected",
                     "done_for_day", "replaced")

# A textbook setup for this strategy: heavy relative volume, a real fresh
# catalyst, up strongly on the day and since the bell, holding above VWAP
# near the high. Whatever the model has learned, it must be willing to buy
# THIS - if it is not, the bot is silently shut and nobody finds out until
# a week of empty sessions has gone by. That has happened, so it is checked
# out loud at startup.
REFERENCE_SETUP = {
    "rvol": 8.0, "day_pct": 15.0, "float_shares": 8e6, "has_news": 1.0,
    "dist_from_hod": 0.5, "change_5": 3.0, "minutes_since_open": 12.0,
    "above_vwap": 1.0, "catalyst_score": 1.0, "catalyst_age": 20.0,
    "gap_pct": 0.0, "open_pct": 8.0,
}


def _bar_ts(bar):
    """Epoch seconds for a bar's timestamp, or None."""
    try:
        return dt.datetime.fromisoformat(
            str(bar.get("t")).replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def _minutes_since_open(now):
    et = now.astimezone(ET)
    open_dt = et.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                         second=0, microsecond=0)
    return (et - open_dt).total_seconds() / 60.0


def features_from_row(row, now):
    """Model features for one HOD-qualified scanner row."""
    return {
        "rvol": row.get("rvol") or 0.0,
        # Recorded for post-hoc analysis only - FEATURE_ORDER decides what
        # the model actually sees, and this one is feed-distorted.
        "day_volume": row.get("day_volume") or 0.0,
        "avg_volume": row.get("avg_volume") or 0.0,
        "day_pct": row.get("day_pct") or 0.0,
        "float_shares": row.get("float_shares") or 0.0,
        "has_news": 1.0 if row.get("has_news") else 0.0,
        "dist_from_hod": row.get("dist_from_hod") or 0.0,
        "change_5": (row.get("changes") or {}).get("5")
                    or (row.get("changes") or {}).get(5) or 0.0,
        "minutes_since_open": _minutes_since_open(now),
        "above_vwap": 1.0 if row.get("above_vwap") else 0.0,
        # How far it gapped: does a big overnight move follow through or fade?
        "gap_pct": row.get("gap_pct") or 0.0,
        # The move since the 9:30 bell, which is not the same as the gap: a
        # stock can open +40% and go nowhere, or open flat and drive.
        "open_pct": row.get("open_pct") or 0.0,
        # How big the reason is, and how fresh - the two things that
        # separate a scalp from a runner.
        "catalyst_score": (row.get("catalyst") or {}).get("score") or 0.0,
        "catalyst_age": min((row.get("catalyst") or {}).get("age_minutes")
                            or 999.0, 999.0),
    }


def journal_alert(journal, ts, row, now, observed, cfg: Config):
    """Record one scanner row as a graded alert.

    Module-level so the historical replay records alerts exactly the way a
    live session does - if these two ever diverge, the model trains on one
    distribution and trades in another.
    """
    setup = row.get("setup") or {}
    # Grade against the stop the bot would REALLY have used, not the raw
    # setup low. technical_stop clamps into the configured risk band - with
    # the band collapsed to a flat 20% it ignores the setup low entirely -
    # so reading setup["stop"] here labelled every alert against an R the
    # bot never risks. That is the train/serve skew this function exists to
    # prevent. A stop too wide to trade still grades on the fallback.
    stop = technical_stop(row["price"], setup.get("stop"), cfg)
    r_dollars = ((row["price"] - stop) if stop
                 else row["price"] * cfg.bot_stop_pct / 100)
    return journal.record_alert(ts, row["symbol"], row["price"], r_dollars,
                                features_from_row(row, now),
                                setup=setup.get("setup"), observed=observed)


def choose_entries(qualified_rows, scorer, trades_today, traded_symbols,
                   day_pnl, now, cfg: Config, score_threshold=None,
                   losses_today=0, open_positions=0, account=None,
                   bankroll=None, budget=None):
    """Best-scored qualifying rows first, never exceeding the daily cap.

    Picks already made in this cycle count against the daily cap, the
    concurrency cap and the remaining `budget`, so one pass cannot open more
    than the account can pay for. The budget is what makes the last slice of
    a balance a part-sized position rather than a refused one: $2,473 opens
    $1,000, $1,000 and $473 and then stops.
    """
    scored = []
    for row in qualified_rows:
        # The momentum criteria say *what* to trade; the pullback says
        # *when*. No setup means the entry has not arrived - buying here
        # would be chasing the high.
        if not row.get("setup"):
            continue
        features = features_from_row(row, now)
        scored.append((scorer.score(features), row, features))
    scored.sort(key=lambda t: -t[0])

    picks, taken = [], set(traded_symbols)
    for score, row, features in scored:
        count = trades_today + len(picks)
        setup = row["setup"]
        stop = technical_stop(row["price"], setup.get("stop"), cfg)
        if stop is None:
            continue                     # risk to the setup low is too wide
        qty, stop = size_position(row["price"], cfg, stop_price=stop,
                                  budget=budget)
        if qty < 1:
            continue                     # no capital left, or too small
        take, _ = should_enter(row["symbol"], price=row["price"], score=score,
                               trades_today=count, traded_symbols=taken,
                               day_pnl=day_pnl, now=now, cfg=cfg,
                               score_threshold=score_threshold,
                               losses_today=losses_today,
                               open_positions=open_positions + len(picks),
                               account=account, notional=qty * row["price"],
                               bankroll=bankroll)
        if not take:
            continue
        picks.append({"symbol": row["symbol"], "price": row["price"],
                      "qty": qty, "stop": stop, "score": score,
                      "setup": setup.get("setup"), "features": features})
        taken.add(row["symbol"])
        if budget is not None:
            budget -= qty * row["price"]
    return picks


def _past(now, hhmm):
    hour, minute = _parse_hhmm(hhmm)
    et = now.astimezone(ET)
    return (et.hour, et.minute) >= (hour, minute)


class TradingBot:
    """Holds bot state; one instance per app run."""

    def __init__(self, cfg: Config, journal: Journal, broker: Broker):
        self.cfg = cfg
        self.journal = journal
        self.broker = broker
        self.open_trades = {}     # symbol -> dict
        self.rejected = set()     # symbols whose entry the broker refused today
        self.open_orders = []     # live broker orders, for the dashboard
        self.account = None       # last good /v2/account snapshot
        # The balance decides how many $1,000 positions fit, not how big
        # each one is. Seeded from config until the first account read
        # lands, which is why _bankroll_seeded exists: the 3x sanity guard
        # in bankroll_from must not measure the first real reading against a
        # number that was never a balance.
        self.bankroll = cfg.bot_bankroll
        self._bankroll_seeded = False
        self._account_pull = 0.0
        self._rejected_day = None
        self.scorer, self.model_meta = self._retrain()
        self.error = None
        self.equity_history = None
        self._flattened_day = None

    def _retrain(self):
        dataset = self.journal.labeled_dataset()
        scorer, meta = train(dataset, min_samples=self.cfg.bot_model_min_samples,
                             percentile=self.cfg.bot_score_percentile)
        # A trained model sets its own bar; the fixed one only fits the
        # heuristic's score range.
        self.score_threshold = meta.get("threshold") or self.cfg.bot_score_threshold
        self.reference_score = scorer.score(REFERENCE_SETUP)
        if self.reference_score < self.score_threshold:
            # Trained on the wrong distribution - stale rows from a previous
            # strategy will teach it that this one's own signals are bad.
            print(f"[bot] !! MODEL REJECTS ITS OWN TEXTBOOK SETUP: "
                  f"scores {self.reference_score:.4f} against a bar of "
                  f"{self.score_threshold:.4f} ({meta['samples']} samples).")
            print("[bot]    Nothing will trade. The training data probably "
                  "predates the current strategy; archive it and let the "
                  "heuristic run until new rows accumulate.")
        else:
            print(f"[bot] scoring: {meta['kind']} bar={self.score_threshold} "
                  f"reference setup scores {self.reference_score:.4f} (passes)")
        if meta["kind"] == "logreg":
            last = self.journal.latest_model()
            if not last or last["samples"] != meta["samples"]:
                self.journal.record_model(
                    int(dt.datetime.now(dt.timezone.utc).timestamp()),
                    meta["samples"], meta["holdout_acc"], meta["weights"])
        return scorer, meta

    # ------------------------------------------------------------ cycle

    async def cycle(self, state, now):
        day = now.astimezone(ET).strftime("%Y-%m-%d")
        ts = int(now.timestamp())
        payload = state.payload(now, require_news=True)
        qualified = payload["hod"]["qualified"]

        # 1. journal every qualified alert + track open alert outcomes
        for row in qualified:
            self._journal_alert(ts, row, now, observed=0)
        if self.cfg.learn_from_near_misses:
            # Rows that missed by exactly one criterion are graded too, but
            # never traded: they teach the model what separates a winner
            # from an almost-winner, without loosening what it buys.
            for row in payload["hod"].get("near") or []:
                self._journal_alert(ts, row, now, observed=1)
        for alert_id, symbol in self.journal.tracking_alerts(day, ts):
            latest = state.latest.get(symbol)
            if latest:
                bar = latest.get("minute_bar") or {}
                self.journal.track_alert(alert_id, ts, latest["price"],
                                         high=bar.get("h"), low=bar.get("l"))

        # 2. manage open trades (fills, time stop, flatten)
        await self._manage_open(state, now, ts)

        # 3. new entries
        if _past(now, self.cfg.bot_flatten_time):
            return
        if self._rejected_day != day:      # fresh slate each session
            self.rejected, self._rejected_day = set(), day
        account = await self._account_snapshot(ts)
        if account:
            self.bankroll = bankroll_from(
                account, self.cfg,
                self.bankroll if self._bankroll_seeded else None)
            self._bankroll_seeded = True
        trades = self.journal.trades_today(day)
        picks = choose_entries(
            qualified, self.scorer,
            trades_today=len(trades),
            traded_symbols={t["symbol"] for t in trades} | self.rejected,
            day_pnl=self.journal.day_pnl(day),
            now=now, cfg=self.cfg,
            score_threshold=self.score_threshold,
            losses_today=self.journal.losses_today(day),
            open_positions=len(self.open_trades),
            account=account, bankroll=self.bankroll,
            budget=self._budget(account))
        for pick in picks:
            try:
                await self._enter(pick, ts)
            except Exception as exc:
                # Don't re-hammer a symbol the broker refused; one line, once.
                self.rejected.add(pick["symbol"])
                print(f"[bot] ENTRY REJECTED {pick['symbol']}: {exc}")

    def _budget(self, account):
        """Capital still free to deploy, in dollars.

        Bounded by EQUITY, never by margin. A paper account reports several
        times its balance as day-trading buying power, and sizing off that
        would open a stack of leveraged positions rather than the three a
        $2,473 balance actually supports. The broker's own figure is applied
        as a second ceiling in case it is the tighter of the two.
        """
        committed = sum(t["qty"] * t["entry"]
                        for t in self.open_trades.values())
        budget = self.bankroll - committed
        power = buying_power(account)
        if power is not None:
            budget = min(budget, power)
        return max(0.0, budget)

    async def _account_snapshot(self, ts):
        """Cached /v2/account, refreshed every 30s. None if never readable.

        Buying power moves with every fill, so it cannot be read once at
        startup - but it does not need re-reading on a 3-second poll either.
        A failed refresh keeps the last good snapshot rather than blocking
        entries: should_enter fails open on a missing account.
        """
        if ts - self._account_pull < 30:
            return self.account
        self._account_pull = ts
        try:
            self.account = await self.broker.account()
        except Exception as exc:
            print(f"[bot] account read failed, using last known: {exc}")
        return self.account

    def _journal_alert(self, ts, row, now, observed):
        journal_alert(self.journal, ts, row, now, observed, self.cfg)

    async def _enter(self, pick, ts):
        entry = pick["price"]
        # One levels function for both paths: the stop sits at the setup's
        # invalidation level and the target is a multiple of that risk. The
        # split is all that differs. This must match
        # scanner.backtest.simulate or the bot trades a strategy the
        # backtest never measured.
        levels = exit_levels(entry, self.cfg, stop_price=pick.get("stop"))
        bank_qty, runner_qty = (bank_split(pick["qty"], self.cfg)
                                if self.cfg.bot_runner_mode
                                else split_qty(pick["qty"]))
        total_qty = pick["qty"]

        # One atomic order: the stop rides along and Alpaca arms it after the
        # fill. Submitting buy and stop separately is rejected as a wash trade
        # ("opposite side market/stop order exists"), which is what kept every
        # entry from going through.
        limit = entry * (1 + self.cfg.bot_limit_slippage_pct / 100)
        parent = await self.broker.submit_oto_stop(
            pick["symbol"], total_qty, levels["stop"], limit_price=limit)

        try:
            trade_id = self.journal.record_trade_open(
                ts, pick["symbol"], qty=total_qty, entry=entry,
                stop=levels["stop"], targets=[levels["scale_out"]],
                features=pick["features"], setup=pick.get("setup"))
        except Exception:
            # The order is already live. An untracked position would miss
            # its scale-out, its time stop and the daily cap, so unwind it
            # rather than leave risk the bot cannot see.
            print(f"[bot] JOURNAL FAILED after entry on {pick['symbol']} - "
                  "unwinding the order")
            try:
                await self.broker.cancel_orders_for(pick["symbol"])
                await self.broker.close_position(pick["symbol"])
            except Exception as unwind:
                print(f"[bot] UNWIND FAILED {pick['symbol']}: {unwind}")
            raise
        self.open_trades[pick["symbol"]] = {
            "trade_id": trade_id, "parent_order_id": parent["id"],
            "trailing_order_id": None, "qty": total_qty,
            "bank_qty": bank_qty, "runner_qty": runner_qty,
            "entry": entry, "signal_price": entry, "stop": levels["stop"],
            "scale_out": levels["scale_out"], "opened_ts": ts,
            # The order is accepted, not filled. Until a position exists this
            # trade is pending: a missing position means "not yet", not
            # "closed". See _settle_pending.
            "filled": False,
            "banked": False}
        print(f"[bot] ENTER {pick['symbol']} x{total_qty} @~{entry:.2f} "
              f"[{pick.get('setup')}] stop {levels['stop']:.2f} "
              f"scale-out {levels['scale_out']:.2f}")

    async def _manage_open(self, state, now, ts):
        if not self.open_trades:
            return
        positions = {p["symbol"]: p for p in await self.broker.positions()}
        flatten = _past(now, self.cfg.bot_flatten_time)
        for symbol, trade in list(self.open_trades.items()):
            pos = positions.get(symbol)
            if pos is None:
                if not trade.get("filled") and not await self._settle_pending(
                        symbol, trade, ts):
                    continue
                exit_price = await self._closed_exit_price(symbol, trade)
                reason = "trailing" if trade["banked"] else "stop"
                self.journal.record_trade_close(trade["trade_id"], ts,
                                                exit_price, reason)
                del self.open_trades[symbol]
                print(f"[bot] CLOSED {symbol} @~{exit_price:.2f} ({reason})")
                continue

            if not trade.get("filled"):
                # The position is proof of the fill, and carries its price.
                self._adopt_fill(symbol, trade, pos.get("avg_entry_price"))

            latest = state.latest.get(symbol)
            price = (latest["price"] if latest
                     else float(pos.get("current_price") or trade["entry"]))

            if flatten:
                await self._flatten_trade(symbol, trade, ts, pos, "flatten")
                continue

            if self.cfg.bot_runner_mode:
                await self._manage_runner(symbol, trade, state, ts, pos, price)
                continue

            if not trade["banked"] and price >= trade["scale_out"]:
                await self.broker.cancel_orders_for(symbol)
                if trade["runner_qty"] >= 1:
                    await self.broker.submit_market_sell(symbol, trade["bank_qty"])
                    tr = await self.broker.submit_trailing_stop(
                        symbol, trade["runner_qty"], self.cfg.bot_runner_trail_pct)
                    trade["trailing_order_id"] = tr["id"]
                else:
                    await self.broker.submit_market_sell(symbol, trade["qty"])
                trade["banked"] = True
                print(f"[bot] SCALE-OUT {symbol}: banked {trade['bank_qty']} "
                      f"@~{price:.2f}, runner {trade['runner_qty']} trailing "
                      f"{self.cfg.bot_runner_trail_pct:g}%")
                continue

            age_min = (ts - trade["opened_ts"]) / 60
            if not trade["banked"] and age_min >= self.cfg.bot_time_stop_minutes:
                await self._flatten_trade(symbol, trade, ts, pos, "time_stop")

    async def _settle_pending(self, symbol, trade, ts):
        """No position yet: is the entry still working, or is it dead?

        Returns True once the entry is known to have filled - with no
        position against it, that means the trade opened and closed between
        two polls, and the caller records the close.

        A trade is registered the moment the OTO order is ACCEPTED, and the
        next cycle runs three seconds later - a marketable limit on a thin
        low-priced name has often not filled by then. Reading "no position"
        as "the trade closed" journalled a phantom exit at the entry price
        (the live journal holds an IVF trade open for exactly one poll cycle)
        and then forgot an order that could still fill, leaving a position
        with only its stop leg: no scale-out, no time stop, no stall exit. So
        an unfilled entry gets asked about rather than assumed.
        """
        try:
            order = await self.broker.order(trade["parent_order_id"])
        except Exception as exc:
            print(f"[bot] entry order unreadable for {symbol}: {exc}")
            return False                 # ask again next cycle
        status = (order or {}).get("status")
        if status in ("filled", "partially_filled"):
            self._adopt_fill(symbol, trade, order.get("filled_avg_price"))
            return True
        if status in DEAD_ORDER_STATES:
            self._drop_pending(symbol, trade,
                               f"entry {status} - nothing was bought")
            return False
        if ts - trade["opened_ts"] >= self.cfg.bot_entry_timeout_seconds:
            # The setup that justified this price is minutes old now. Pull the
            # order rather than let it fill into a different market. A fill
            # racing the cancel is left to the flatten job to reconcile.
            try:
                await self.broker.cancel_orders_for(symbol)
            except Exception as exc:
                print(f"[bot] cancelling the unfilled entry failed "
                      f"{symbol}: {exc}")
                return False
            self._drop_pending(
                symbol, trade,
                f"unfilled after {self.cfg.bot_entry_timeout_seconds}s")
        return False

    def _drop_pending(self, symbol, trade, why):
        """Forget an entry that bought nothing, journal row included."""
        try:
            self.journal.delete_trade(trade["trade_id"])
        except Exception as exc:
            print(f"[bot] could not remove the pending trade row: {exc}")
        self.open_trades.pop(symbol, None)
        print(f"[bot] ENTRY DROPPED {symbol}: {why}")

    def _adopt_fill(self, symbol, trade, filled_price):
        """Mark the entry filled and record what was actually paid.

        The stop and the target stay where they were placed: the stop is a
        live broker order riding along with the entry, and moving the target
        after the fact would make the live path measure a different trade
        from the one the backtest simulates.
        """
        trade["filled"] = True
        try:
            price = float(filled_price)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0 or abs(price - trade["entry"]) < 0.005:
            return
        trade["entry"] = price
        self.journal.update_trade_entry(trade["trade_id"], price)
        print(f"[bot] FILLED {symbol} @{price:.2f} "
              f"(signalled {trade['signal_price']:.2f})")

    def _stalled(self, state, symbol, opened_ts):
        """Have the last N completed bars all been dojis?

        A doji opens and closes in the same place: buyers and sellers
        balanced, the move out of steam. Read off completed bars only - the
        minute in progress is replaced on every poll and would flicker in
        and out of being a doji. Bars from before the entry do not count.
        """
        history = getattr(state, "histories", {}).get(symbol)
        if history is None:
            return False
        want = self.cfg.bot_doji_exit_bars
        bars = [b for b in history.completed_bars
                if (_bar_ts(b) or 0) > opened_ts][-want:]
        return len(bars) == want and all(is_doji(b, self.cfg) for b in bars)

    async def _protect_runner(self, symbol, trade, price):
        """Lift the runner's stop once the bulk is banked. Returns a label.

        A trailing stop ratchets up behind the high water mark, so a runner
        that keeps running keeps more of it - the fixed break-even stop used
        to hand back every cent above entry the moment price came off. The
        width is capped by runner_trail_pct so the first stop can never sit
        below what was paid.
        """
        breakeven = round(trade["entry"], 2)
        trade["stop"] = breakeven         # the floor, whatever the trail does
        pct = (runner_trail_pct(trade["entry"], price, self.cfg)
               if self.cfg.bot_runner_uses_trail else None)
        if pct is None:
            await self.broker.submit_stop(symbol, trade["runner_qty"], breakeven)
            return f"stop at break-even {breakeven:.2f}"
        order = await self.broker.submit_trailing_stop(
            symbol, trade["runner_qty"], pct)
        trade["trailing_order_id"] = (order or {}).get("id")
        return f"trailing {pct:g}%, never below break-even {breakeven:.2f}"

    async def _manage_runner(self, symbol, trade, state, ts, pos, price):
        """Fixed-cent target, then the runner rides until it stalls.

        Deliberately different from the simulator in two places. The stop is
        not checked here because it is a live broker order riding along with
        the entry OTO, which fires without us. And the target is compared
        against the last polled price rather than the bar high, because a
        session cannot see the high of a minute still in progress - the
        backtest can, which is why its scalp results are an upper bound.
        """
        if not trade["banked"] and price >= trade["scale_out"]:
            await self.broker.cancel_orders_for(symbol)
            if trade["runner_qty"] >= 1:
                await self.broker.submit_market_sell(symbol, trade["bank_qty"])
                protection = await self._protect_runner(symbol, trade, price)
                trade["banked"] = True
                print(f"[bot] SCALE-OUT {symbol}: banked {trade['bank_qty']} "
                      f"@~{price:.2f}, runner {trade['runner_qty']} "
                      f"{protection}")
            else:
                await self.broker.submit_market_sell(symbol, trade["qty"])
                trade["banked"] = True
                print(f"[bot] TARGET {symbol}: sold {trade['qty']} @~{price:.2f}")
            return

        if self._stalled(state, symbol, trade["opened_ts"]):
            await self._flatten_trade(symbol, trade, ts, pos, "stall")
            return

        # The clock is for a position that has not paid yet. A banked runner
        # is playing with the market's money behind a trailing stop, and
        # cutting it at ten minutes was throwing away the only part of this
        # strategy that can make more than 20c.
        if (not trade["banked"]
                and (ts - trade["opened_ts"]) / 60
                >= self.cfg.bot_time_stop_minutes):
            await self._flatten_trade(symbol, trade, ts, pos, "time_stop")

    async def _flatten_trade(self, symbol, trade, ts, pos, reason):
        # Clear protective orders first: an open sell blocks the close as a
        # wash trade, and a leftover one blocks tomorrow's entry.
        await self.broker.cancel_orders_for(symbol)
        await self.broker.close_position(symbol)
        fallback = float(pos.get("current_price") or trade["entry"])
        exit_price = await self._closed_exit_price(symbol, trade, fallback)
        self.journal.record_trade_close(trade["trade_id"], ts, exit_price, reason)
        del self.open_trades[symbol]
        print(f"[bot] CLOSED {symbol} @~{exit_price:.2f} ({reason})")

    async def _closed_exit_price(self, symbol, trade, fallback=None):
        """Share-weighted average of THIS trade's closed sell fills.

        Bounded by the trade's own open time. Unbounded, the query answered
        with the 50 newest closed orders for the symbol whenever they
        happened, so a symbol traded on two different days had yesterday's
        exits averaged into today's R multiple.
        """
        legs = await self.broker.closed_sell_legs(symbol, trade["opened_ts"])
        avg = weighted_exit(legs)
        if avg is not None:
            return avg
        return fallback if fallback is not None else trade["entry"]

    # ------------------------------------------------------------ status

    def status(self, day):
        trades = self.journal.trades_today(day)
        return {
            "enabled": True,
            "error": self.error,
            "bankroll": self.bankroll,
            # How the balance splits into positions, so the dashboard states
            # the sizing in force instead of implying one trade holds it all.
            "position_dollars": self.cfg.bot_position_dollars,
            "slots": position_slots(self.bankroll, self.cfg),
            # What an alert is graded against, so the dashboard cannot drift
            # out of step with the strategy the way a hardcoded target did.
            "target_r": self.cfg.bot_scale_out_r,
            "trades_today": len(trades),
            "cap": self.cfg.bot_max_trades_per_day,
            "day_pnl": self.journal.day_pnl(day),
            "open": [{"symbol": s, **{k: v for k, v in t.items()
                                      if k != "order_ids"}}
                     for s, t in self.open_trades.items()],
            "today": trades,
            "recent": self.journal.recent_trades(50),
            "stats": self.journal.rolling_stats(20),
            "model": {k: v for k, v in self.model_meta.items()
                      if k != "weights"},
            "score_threshold": round(self.score_threshold, 3),
            "model_history": self.journal.model_history(10),
            "alerts": self.journal.recent_alerts(40),
            "learning": self.journal.learning_progress(
                self.cfg.bot_model_min_samples),
            "setup_stats": self.journal.setup_stats(),
            "orders": self.open_orders,
            "equity": self.equity_history,
        }


async def bot_loop(app, cfg: Config):
    ctx = app["ctx"]
    async with aiohttp.ClientSession() as session:
        broker = Broker(session, cfg)   # PaperOnlyError if misconfigured
        account = await broker.account()
        equity = float(account["equity"])
        print(f"[bot] paper account ok — equity ${equity:,.2f}, "
              f"{position_slots(equity, cfg)} position(s) of "
              f"${cfg.bot_position_dollars:,.0f} "
              f"(max {cfg.bot_max_concurrent_positions})")
        # No cent target any more: WIN_R grades the same 2R the bot trades for.
        journal = Journal(cfg.bot_journal_path, cfg.bot_alert_window_minutes)
        bot = TradingBot(cfg, journal, broker)
        last_equity_pull = 0.0

        while True:
            now = dt.datetime.now(dt.timezone.utc)
            try:
                await bot.cycle(ctx["state"], now)
                if now.timestamp() - last_equity_pull > 300:
                    history = await broker.portfolio_history()
                    bot.equity_history = [
                        [t, e] for t, e in zip(history.get("timestamp") or [],
                                               history.get("equity") or [])
                        if e is not None]
                    bot.open_orders = await broker.open_orders() or []
                    last_equity_pull = now.timestamp()
                bot.error = None
            except Exception as exc:
                bot.error = str(exc)
                traceback.print_exc()
            day = now.astimezone(ET).strftime("%Y-%m-%d")
            ctx["bot_status"] = bot.status(day)
            await asyncio.sleep(cfg.poll_seconds)
