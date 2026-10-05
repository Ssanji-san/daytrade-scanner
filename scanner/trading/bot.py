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
from ..setups import vwap
from .broker import Broker
from .journal import Journal
from .model import train
from .strategy import (ET, MARKET_OPEN, _parse_hhmm, bankroll_from,
                       buying_power, candle_exit, exit_levels, is_doji,
                       is_premarket, position_slots, premarket_entry_limit,
                       premarket_exit_limit, runner_trail_pct, scalp_levels,
                       scalp_split, should_enter, size_position, split_qty,
                       technical_stop, weighted_exit)

STARTUP_ATTEMPTS = 10

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
                                setup=setup.get("setup"), observed=observed,
                                failed=row.get("failed"))


def _held(pos):
    """Signed shares the broker reports, or None when it reports none."""
    try:
        return int(float(pos.get("qty")))
    except (AttributeError, TypeError, ValueError):
        return None


def _position_qty(pos, trade):
    """Shares there are to sell, from the broker; the journal's view if absent.

    A pre-market exit sells what is really there. A scale-out limit that
    only partly filled leaves more than runner_qty behind, and the stop has
    to take all of it.

    A short is zero, never its size: every exit here is a sell, and reading
    -50 as 50 would sell fifty more and double the short.
    """
    held = _held(pos)
    if held is None:
        return trade["runner_qty"] if trade["banked"] else trade["qty"]
    return max(0, held)


def choose_entries(qualified_rows, scorer, trades_today, traded_symbols,
                   day_pnl, now, cfg: Config, score_threshold=None,
                   losses_today=0, open_positions=0, account=None,
                   bankroll=None, budget=None, skips=None):
    """Best-scored qualifying rows first, never exceeding the daily cap.

    Picks already made in this cycle count against the daily cap, the
    concurrency cap and the remaining `budget`, so one pass cannot open more
    than the account can pay for. The budget is what makes the last slice of
    a balance a part-sized position rather than a refused one: $2,473 opens
    $1,000, $1,000 and $473 and then stops.

    Pass a list as `skips` to learn why the rest were left alone. Every
    reason here was already being computed and thrown away, and the alert
    is tracked to its outcome regardless of whether the bot buys it - so
    recording the decision is what makes "which rule blocked a winner" a
    question the journal can answer.
    """
    def note(symbol, reason, score=None):
        if skips is not None:
            skips.append({"symbol": symbol, "reason": reason, "score": score})

    scored = []
    for row in qualified_rows:
        # The momentum criteria say *what* to trade; the pullback says
        # *when*. No setup means the entry has not arrived - buying here
        # would be chasing the high.
        if not row.get("setup"):
            note(row["symbol"], "no_setup")
            continue
        features = features_from_row(row, now)
        scored.append((scorer.score(features), row, features))
    scored.sort(key=lambda t: -t[0])

    picks, taken = [], set(traded_symbols)
    premarket = is_premarket(now)
    for score, row, features in scored:
        count = trades_today + len(picks)
        setup = row["setup"]
        stop = technical_stop(row["price"], setup.get("stop"), cfg)
        if stop is None:
            note(row["symbol"], "stop_too_wide", score)
            continue                     # risk to the setup low is too wide
        # Pre-market the order is a limit at the ask plus Ross's offset, and
        # the position is sized on that limit so a fill at the very top of it
        # still risks the intended amount. Regular hours are untouched: sized
        # on the signal, entered by OTO.
        limit = (premarket_entry_limit(row["price"], row.get("ask"), cfg)
                 if premarket else None)
        cost = limit or row["price"]
        qty, stop = size_position(cost, cfg, stop_price=stop, budget=budget)
        if qty < 1:
            note(row["symbol"], "no_capital", score)
            continue                     # no capital left, or too small
        take, reasons = should_enter(
            row["symbol"], price=row["price"], score=score,
            trades_today=count, traded_symbols=taken,
            day_pnl=day_pnl, now=now, cfg=cfg,
            score_threshold=score_threshold,
            losses_today=losses_today,
            open_positions=open_positions + len(picks),
            account=account, notional=qty * cost,
            bankroll=bankroll)
        if not take:
            note(row["symbol"], "+".join(reasons), score)
            continue
        picks.append({"symbol": row["symbol"], "price": row["price"],
                      "premarket": premarket, "limit": limit,
                      "qty": qty, "stop": stop, "score": score,
                      "setup": setup.get("setup"), "features": features})
        taken.add(row["symbol"])
        if budget is not None:
            budget -= qty * cost
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
        skips = []
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
            budget=self._budget(account), skips=skips)
        # Record what was decided about every qualifying row, taken or not.
        # The alert is already tracked to its outcome, so this is the half
        # that was missing: not what the stock did, but what the bot did.
        for skip in skips:
            self.journal.record_decision(ts, skip["symbol"], skip["reason"])
        for pick in picks:
            try:
                await self._enter(pick, ts)
            except Exception as exc:
                # Don't re-hammer a symbol the broker refused; one line, once.
                self.rejected.add(pick["symbol"])
                self.journal.record_decision(ts, pick["symbol"],
                                             "broker_rejected")
                print(f"[bot] ENTRY REJECTED {pick['symbol']}: {exc}")
            else:
                # Only after the broker took it. "taken" is the top of the
                # ladder and never downgrades, so claiming it for an order
                # that was refused would be a lie the journal keeps.
                self.journal.record_decision(ts, pick["symbol"], "taken")

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
        # Scalping takes profit a fixed number of cents above entry and
        # banks most of the position there; the swing path scales at +2R.
        # This must match scanner.backtest.simulate or the bot trades a
        # strategy the backtest never measured.
        if self.cfg.bot_scalp_mode:
            scalp = scalp_levels(entry, self.cfg, stop_price=pick.get("stop"))
            levels = {"stop": scalp["stop"], "scale_out": scalp["target"]}
            bank_qty, runner_qty = scalp_split(pick["qty"], self.cfg)
        else:
            levels = exit_levels(entry, self.cfg, stop_price=pick.get("stop"))
            bank_qty, runner_qty = split_qty(pick["qty"])
        total_qty = pick["qty"]
        premarket = bool(pick.get("premarket"))

        if premarket:
            # Before the bell Alpaca takes only extended-hours limit orders -
            # no OTO, so no stop can ride along. The bot runs this position's
            # stop itself (_manage_premarket) until the bell, when it hands
            # it to the broker (_hand_off_at_bell).
            limit = pick["limit"]
            parent = await self.broker.submit_limit_buy(
                pick["symbol"], total_qty, limit, extended_hours=True)
        else:
            # One atomic order: the stop rides along and Alpaca arms it after
            # the fill. Submitting buy and stop separately is rejected as a
            # wash trade ("opposite side market/stop order exists"), which is
            # what kept every entry from going through.
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
            # True while the bot, not the broker, holds this position's stop.
            "managed_stop": premarket, "limit": limit,
            # The order is accepted, not filled. Until a position exists this
            # trade is pending: a missing position means "not yet", not
            # "closed". See _settle_pending.
            "filled": False,
            "banked": False}
        how = (f"pre-market limit {limit:.2f} (ext. hours), stop run by the bot"
               if premarket else f"limit {limit:.2f}")
        exits = ("exit on a candle indicator"
                 if self.cfg.bot_exit_mode == "candle"
                 else f"scale-out {levels['scale_out']:.2f}")
        print(f"[bot] ENTER {pick['symbol']} x{total_qty} @~{entry:.2f} "
              f"[{pick.get('setup')}] {how} stop {levels['stop']:.2f} "
              f"{exits}")

    async def _manage_open(self, state, now, ts):
        if not self.open_trades:
            return
        positions = {p["symbol"]: p for p in await self.broker.positions()}
        flatten = _past(now, self.cfg.bot_flatten_time)
        for symbol, trade in list(self.open_trades.items()):
            # One position's broker error must not cost the others their
            # stop check. The loop used to abort on the first exception, and
            # a refusal that repeats every cycle starved every position after
            # it - fatal for one whose only stop is this loop.
            try:
                await self._manage_one(symbol, trade, positions.get(symbol),
                                       state, now, ts, flatten)
            except Exception as exc:
                print(f"[bot] managing {symbol} failed, the rest carry on: "
                      f"{exc!r}")

    async def _manage_one(self, symbol, trade, pos, state, now, ts, flatten):
        if pos is None:
            just_filled = False
            if not trade.get("filled"):
                if not await self._settle_pending(symbol, trade, ts):
                    return
                just_filled = True
            exit_price = await self._closed_exit_price(symbol, trade)
            if exit_price is None:
                if just_filled:
                    # Filled after positions() was read, and nothing has been
                    # sold: the position exists, this poll just missed it.
                    # Closing here journalled a 0R exit at the entry price and
                    # dropped a live position - pre-market, one with no stop.
                    return
                exit_price = trade["entry"]
            # A pre-market exit is sent as a limit and recorded here, when
            # the position is actually gone, under the reason it was sent.
            reason = (trade.get("exit") or {}).get("reason") or (
                "trailing" if trade["banked"] else "stop")
            self.journal.record_trade_close(trade["trade_id"], ts,
                                            exit_price, reason)
            del self.open_trades[symbol]
            print(f"[bot] CLOSED {symbol} @~{exit_price:.2f} ({reason})")
            return

        held = _held(pos)
        if held is not None and held < 0:
            # Every exit this bot sends is a sell, so any of them would add
            # to a short. Nothing it does can make this better; a person can.
            if not trade.get("short_reported"):
                trade["short_reported"] = True
                print(f"[bot] !! SHORT {held} {symbol}: the account is short. "
                      "The bot will not sell against it - cover it by hand.")
            return

        if not trade.get("filled"):
            # The position is proof of the fill, and carries its price.
            self._adopt_fill(symbol, trade, pos.get("avg_entry_price"))

        latest = state.latest.get(symbol)
        price = (latest["price"] if latest
                 else float(pos.get("current_price") or trade["entry"]))

        if flatten:
            await self._flatten_trade(symbol, trade, ts, pos, "flatten")
            return

        if trade.get("managed_stop"):
            if is_premarket(now):
                await self._manage_premarket(symbol, trade, state, now,
                                             ts, pos, price)
            else:
                await self._hand_off_at_bell(symbol, trade, ts, pos, price)
            return

        if self.cfg.bot_exit_mode == "candle":
            reason = self._candle_signal(state, symbol, trade)
            if reason:
                await self._flatten_trade(symbol, trade, ts, pos, reason)
            return

        if self.cfg.bot_scalp_mode:
            await self._manage_scalp(symbol, trade, state, ts, pos, price)
            return

        if not trade["banked"] and price >= trade["scale_out"]:
            await self.broker.cancel_orders_for(symbol)
            held = await self._held_now(symbol, trade, fallback=pos)
            if held < 1:
                return
            self._resplit(trade, held)
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
            return

        age_min = (ts - trade["opened_ts"]) / 60
        if not trade["banked"] and age_min >= self.cfg.bot_time_stop_minutes:
            await self._flatten_trade(symbol, trade, ts, pos, "time_stop")

    async def _held_now(self, symbol, trade, fallback=None):
        """Shares held right now - re-read, because a cancel just went out.

        The cycle's position snapshot predates the cancel. An order that
        filled in between (the tail of a partly filled entry, or an exit
        racing its own cancel) changed what is there to sell. 0 when the
        position is gone or short.

        If the re-read fails: with a `fallback` snapshot, use it - in regular
        hours the broker stop was just cancelled, and aborting now would
        leave the position with neither the stop nor the sale. Without one
        the error propagates and the cycle retries, which is only safe where
        the bot, not the broker, holds the stop.
        """
        try:
            positions = await self.broker.positions() or []
        except Exception as exc:
            if fallback is None:
                raise
            print(f"[bot] {symbol}: position re-read failed ({exc!r}); "
                  "using this cycle's snapshot")
            return _position_qty(fallback, trade)
        for pos in positions:
            if pos["symbol"] == symbol:
                return _position_qty(pos, trade)
        return 0

    def _resplit(self, trade, held):
        """Split the shares actually held into bank and runner.

        The split was fixed when the order went in, on the size ASKED for. A
        limit that filled 80 of 200 then tried to bank 130: refused, or on a
        margin account a short. The journal's quantity follows, since P&L
        multiplies by it.
        """
        if held != trade["qty"]:
            trade["qty"] = held
            self.journal.update_trade_qty(trade["trade_id"], held)
        trade["bank_qty"], trade["runner_qty"] = (
            scalp_split(held, self.cfg) if self.cfg.bot_scalp_mode
            else split_qty(held))

    async def _clear_then_count(self, symbol, trade):
        """Cancel this symbol's orders and wait for them to settle, then
        return how many shares there are to sell.

        0 means send nothing this cycle: the cancel has not settled (its
        shares are still reserved and a sell would be refused), or there is
        no long position left. Only for positions whose stop the bot holds
        itself: the orders being cancelled are its own limits, never a broker
        stop, so waiting a cycle leaves nothing less protected than it was.
        """
        cleared = await self.broker.cancel_orders_for(
            symbol, settle_seconds=self.cfg.bot_cancel_settle_seconds)
        if not cleared:
            print(f"[bot] {symbol}: cancel not settled yet, sell waits a cycle")
            return 0
        return await self._held_now(symbol, trade)

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
        try:
            # "taken" was recorded when the broker accepted the order; it
            # never filled, so it was never taken. This is what makes the
            # fill rate - and whether the misses would have won - a query.
            self.journal.record_decision(trade["opened_ts"], symbol,
                                         "unfilled", override=True)
        except Exception as exc:
            print(f"[bot] could not record the unfilled entry: {exc}")
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

    def _candle_signal(self, state, symbol, trade):
        """Ross's chart exit indicator on the newest completed candle since
        the entry, or None.

        Completed bars only, as the replay judges them: the minute still in
        progress is replaced on every poll and would flicker in and out of
        being a red candle. The stop is not checked here - regular hours it
        is a broker order, pre-market _manage_premarket checks it first.
        """
        history = getattr(state, "histories", {}).get(symbol)
        bars = history.completed_bars if history is not None else []
        if not bars or (_bar_ts(bars[-1]) or 0) <= trade["opened_ts"]:
            return None
        prev = bars[-2] if len(bars) > 1 else None
        return candle_exit(bars[-1], prev, vwap(bars), self.cfg)

    def _arm_runner(self, trade, price):
        """The runner's rules once the bulk is banked. Returns the trail %.

        The stop goes to break-even - the floor, whatever the trail does -
        and the trail width is capped by runner_trail_pct so the first stop
        can never sit below what was paid. One definition for both sides of
        the bell: who holds the stop differs, the rule must not.
        """
        trade["stop"] = round(trade["entry"], 2)
        trade["trail_pct"] = (runner_trail_pct(trade["entry"], price, self.cfg)
                              if self.cfg.bot_scalp_runner_trail else None)
        trade["high"] = price
        return trade["trail_pct"]

    async def _protect_runner(self, symbol, trade, price):
        """Lift the runner's stop once the bulk is banked. Returns a label.

        A trailing stop ratchets up behind the high water mark, so a runner
        that keeps running keeps more of it - the fixed break-even stop used
        to hand back every cent above entry the moment price came off.
        """
        pct = self._arm_runner(trade, price)
        breakeven = trade["stop"]
        if pct is None:
            await self.broker.submit_stop(symbol, trade["runner_qty"], breakeven)
            return f"stop at break-even {breakeven:.2f}"
        order = await self.broker.submit_trailing_stop(
            symbol, trade["runner_qty"], pct)
        trade["trailing_order_id"] = (order or {}).get("id")
        return f"trailing {pct:g}%, never below break-even {breakeven:.2f}"

    async def _manage_scalp(self, symbol, trade, state, ts, pos, price):
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
            held = await self._held_now(symbol, trade, fallback=pos)
            if held < 1:
                return
            self._resplit(trade, held)
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

    # ------------------------------------------------ pre-market positions
    #
    # Before 09:30 Alpaca accepts only extended-hours limit orders, so the
    # broker will hold no stop, no trailing stop and no market exit. The bot
    # runs all three itself, and every order it sends here is a limit.

    async def _manage_premarket(self, symbol, trade, state, now, ts, pos, price):
        """One cycle of a position whose stop only the bot can enforce.

        The order matters. An exit already working is chased first. A stale
        price closes the position, because a stop is only as live as the
        price it watches. Then the stop - which must never be skipped - and
        only after it the target, the stall and the clock.
        """
        if trade.get("exit"):
            await self._chase_exit(symbol, trade, state, ts, pos, price)
            return
        if not self._fresh(state, symbol, now):
            broker_px = float(pos.get("current_price") or price)
            await self._premarket_exit(symbol, trade, state, ts, pos,
                                       broker_px, "stale")
            return
        if trade["banked"]:
            self._trail_runner(trade, price)
        if price <= trade["stop"]:
            await self._premarket_exit(symbol, trade, state, ts, pos, price,
                                       "trailing" if trade["banked"] else "stop")
            return
        if self.cfg.bot_exit_mode == "candle":
            reason = self._candle_signal(state, symbol, trade)
            if reason:
                await self._premarket_exit(symbol, trade, state, ts, pos,
                                           price, reason)
            return
        if not trade["banked"] and price >= trade["scale_out"]:
            await self._premarket_scale_out(symbol, trade, state, ts, pos, price)
            return
        if self._stalled(state, symbol, trade["opened_ts"]):
            await self._premarket_exit(symbol, trade, state, ts, pos, price,
                                       "stall")
            return
        if (not trade["banked"]
                and (ts - trade["opened_ts"]) / 60
                >= self.cfg.bot_time_stop_minutes):
            await self._premarket_exit(symbol, trade, state, ts, pos, price,
                                       "time_stop")

    def _fresh(self, state, symbol, now):
        """Has this symbol's tape printed recently enough to trust its price?

        Measured from the last TRADE, not from the poll. The poller refreshes
        every held symbol every few seconds whether or not anything traded,
        so a thin name could stop printing at $2.00 while the market slid to
        $1.80 and still read as fresh on every cycle - a stop that could not
        see. Held symbols also leave the snapshot set candidate_ttl_minutes
        after leaving the screener lists; a runner that outlives that goes
        stale too. Either way it is closed: upside given up, never unguarded.

        Replay and demo feeds carry no trade time and fall back to when the
        price last arrived. A live row that carries the field but no value
        is stale: there is no print to vouch for the price.
        """
        latest = (getattr(state, "latest", {}) or {}).get(symbol) or {}
        if "trade_ts" in latest:
            printed = latest["trade_ts"]
            return bool(printed) and (now.timestamp() - printed
                                      <= self.cfg.bot_stale_quote_seconds)
        history = getattr(state, "histories", {}).get(symbol)
        last = history.latest if history is not None else None
        if not last:
            return False
        age = (now - last[0]).total_seconds()
        return age <= self.cfg.bot_stale_quote_seconds

    def _trail_runner(self, trade, price):
        """Ratchet the runner's stop up behind the high. Never down."""
        trade["high"] = max(trade.get("high") or price, price)
        pct = trade.get("trail_pct")
        if pct:
            trailed = round(trade["high"] * (1 - pct / 100), 2)
            trade["stop"] = max(trade["stop"], trailed)

    def _bid(self, state, symbol):
        return (getattr(state, "latest", {}).get(symbol) or {}).get("bid")

    async def _premarket_exit(self, symbol, trade, state, ts, pos, price,
                              reason):
        """Send the exit as an extended-hours limit, and do NOT record it.

        _flatten_trade records the close straight away because a market
        order fills; a limit may not. The close is written by _manage_open
        once the position is really gone, and _chase_exit re-prices this
        order if it sits unfilled. If a cancel has not settled, nothing is
        sent and no exit is recorded as working: the next cycle checks the
        stop again and tries again.
        """
        qty = await self._clear_then_count(symbol, trade)
        if qty < 1:
            return
        px = premarket_exit_limit(price, self._bid(state, symbol), self.cfg)
        order = await self.broker.submit_limit_sell(symbol, qty, px,
                                                    extended_hours=True)
        trade["exit"] = {"reason": reason, "order_id": (order or {}).get("id"),
                         "ts": ts, "px": px}
        print(f"[bot] EXIT {symbol} ({reason}): limit sell {qty} "
              f"floor {px:.2f}, extended hours")

    async def _chase_exit(self, symbol, trade, state, ts, pos, price):
        """An exit limit that has not filled is re-priced one offset lower.

        Each attempt is at least one offset under the one before and never
        above the current bid less the offset, so a stop keeps walking down
        a thin book until something takes it rather than sitting unfilled
        while the price falls away.
        """
        pending = trade["exit"]
        if ts - pending["ts"] < self.cfg.bot_premarket_chase_seconds:
            return
        # The cancel can race a fill of the very order being cancelled. The
        # re-read after it settles is what stops a re-priced sell going out
        # for shares that were just sold - on a margin account, a short.
        qty = await self._clear_then_count(symbol, trade)
        if qty < 1:
            return
        fresh = premarket_exit_limit(price, self._bid(state, symbol), self.cfg)
        px = max(0.01, round(min(pending["px"], fresh)
                             - self.cfg.bot_premarket_offset_cents, 2))
        order = await self.broker.submit_limit_sell(symbol, qty, px,
                                                    extended_hours=True)
        pending.update(order_id=(order or {}).get("id"), ts=ts, px=px)
        print(f"[bot] CHASE {symbol}: exit unfilled, re-priced to floor "
              f"{px:.2f}")

    async def _premarket_scale_out(self, symbol, trade, state, ts, pos, price):
        """Bank most of it at the target; the runner's stop goes to entry.

        Split on what is actually held: the bank and runner sizes were fixed
        on the size ordered, and a partly filled limit holds less.
        """
        held = await self._clear_then_count(symbol, trade)
        if held < 1:
            return
        self._resplit(trade, held)
        if trade["runner_qty"] < 1:
            await self._premarket_exit(symbol, trade, state, ts, pos, price,
                                       "target")
            return
        px = premarket_exit_limit(price, self._bid(state, symbol), self.cfg)
        await self.broker.submit_limit_sell(symbol, trade["bank_qty"], px,
                                            extended_hours=True)
        trade["banked"] = True
        self._arm_runner(trade, price)
        print(f"[bot] SCALE-OUT {symbol}: banked {trade['bank_qty']} limit "
              f"floor {px:.2f}, runner {trade['runner_qty']} on a bot-run "
              f"trail from {trade['stop']:.2f}")

    async def _hand_off_at_bell(self, symbol, trade, ts, pos, price):
        """At 09:30 give the stop back to the broker.

        From the bell Alpaca takes stop and trailing orders again, and a
        broker-held stop survives this process dying where a bot-run one
        does not. An exit still working is finished at market.

        The stop is checked BEFORE the handoff, every cycle until one
        succeeds. A price already through it would make the broker refuse a
        sell stop above the market, and while the handoff kept failing the
        position had no stop from either side - the bot had stopped watching
        at 09:30 and the broker never started.

        The handoff never lowers protection: a runner goes to a native
        trailing stop only if its trail would start at or above the stop the
        bot was holding, and otherwise to a fixed stop at that level.
        """
        if trade.get("exit"):
            await self._flatten_trade(symbol, trade, ts, pos,
                                      trade["exit"]["reason"])
            return
        if price <= trade["stop"]:
            await self._flatten_trade(symbol, trade, ts, pos,
                                      "trailing" if trade["banked"] else "stop")
            return
        # A resting pre-market limit (an unfilled bank) still reserves its
        # shares; a stop sent over it would be refused.
        qty = await self._clear_then_count(symbol, trade)
        if qty < 1:
            return
        pct = trade.get("trail_pct") if trade["banked"] else None
        if pct and round(price * (1 - pct / 100), 2) >= trade["stop"]:
            order = await self.broker.submit_trailing_stop(symbol, qty, pct)
            trade["trailing_order_id"] = (order or {}).get("id")
            how = f"trailing {pct:g}%"
        else:
            await self.broker.submit_stop(symbol, qty, trade["stop"])
            how = f"stop {trade['stop']:.2f}"
        trade["managed_stop"] = False
        print(f"[bot] BELL {symbol}: stop handed to the broker ({how})")

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
        # None when nothing was sold and the caller gave no fallback. "No
        # sell fills" and "sold at the entry price" must not look the same:
        # conflating them is how a live position got journalled as a 0R close.
        return avg if avg is not None else fallback

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
            # out of step with the strategy the way a hardcoded "+2R" did.
            "target_cents": (self.cfg.bot_scalp_target_cents
                             if self.cfg.bot_scalp_mode else None),
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
        # A dead bot now ends the whole session (see session.run_for), so a
        # network blip at 07:30 must not cost the day. A real fault - bad
        # keys, say - still surfaces after the retries.
        for attempt in range(1, STARTUP_ATTEMPTS + 1):
            try:
                account = await broker.account()
                break
            except Exception as exc:
                if attempt == STARTUP_ATTEMPTS:
                    raise
                print(f"[bot] account read failed ({exc}); retry "
                      f"{attempt}/{STARTUP_ATTEMPTS - 1} in 30s")
                await asyncio.sleep(30)
        equity = float(account["equity"])
        print(f"[bot] paper account ok — equity ${equity:,.2f}, "
              f"{position_slots(equity, cfg)} position(s) of "
              f"${cfg.bot_position_dollars:,.0f} "
              f"(max {cfg.bot_max_concurrent_positions})")
        journal = Journal(cfg.bot_journal_path, cfg.bot_alert_window_minutes,
                          win_target_cents=(cfg.bot_scalp_target_cents
                                            if cfg.bot_scalp_mode else None))
        bot = TradingBot(cfg, journal, broker)
        last_equity_pull = 0.0

        while True:
            now = dt.datetime.now(dt.timezone.utc)
            try:
                await bot.cycle(ctx["state"], now)
                # The scanner keeps these snapshotted (main.watchlist).
                ctx["held"] = set(bot.open_trades)
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
