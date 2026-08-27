"""The trading bot loop: journal alerts, pick entries, manage exits.

Decision logic lives in strategy.py/model.py (tested); this module is the
orchestration around them plus two pure, tested helpers
(features_from_row, choose_entries). Paper account only — Broker enforces it.
"""
import asyncio
import datetime as dt
import traceback
from zoneinfo import ZoneInfo

import aiohttp

from ..config import Config
from .broker import Broker
from .journal import Journal
from .model import train, scorer_from_weights
from .strategy import (ET, exit_levels, should_enter, size_position,
                       split_qty, technical_stop, weighted_exit,
                       _parse_hhmm)

MARKET_OPEN = dt.time(9, 30)


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
        # How big the reason is, and how fresh - the two things that
        # separate a scalp from a runner.
        "catalyst_score": (row.get("catalyst") or {}).get("score") or 0.0,
        "catalyst_age": min((row.get("catalyst") or {}).get("age_minutes")
                            or 999.0, 999.0),
    }


def choose_entries(qualified_rows, scorer, trades_today, traded_symbols,
                   day_pnl, now, cfg: Config, score_threshold=None):
    """Best-scored qualifying rows first, never exceeding the daily cap."""
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
        take, _ = should_enter(row["symbol"], price=row["price"], score=score,
                               trades_today=count, traded_symbols=taken,
                               day_pnl=day_pnl, now=now, cfg=cfg,
                               score_threshold=score_threshold)
        if not take:
            continue
        setup = row["setup"]
        stop = technical_stop(row["price"], setup.get("stop"), cfg)
        if stop is None:
            continue                     # risk to the setup low is too wide
        qty, stop = size_position(row["price"], cfg, stop_price=stop)
        if qty < 1:
            continue
        picks.append({"symbol": row["symbol"], "price": row["price"],
                      "qty": qty, "stop": stop, "score": score,
                      "setup": setup.get("setup"), "features": features})
        taken.add(row["symbol"])
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
        for alert_id, symbol in self.journal.open_alerts():
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
        trades = self.journal.trades_today(day)
        picks = choose_entries(
            qualified, self.scorer,
            trades_today=len(trades),
            traded_symbols={t["symbol"] for t in trades} | self.rejected,
            day_pnl=self.journal.day_pnl(day),
            now=now, cfg=self.cfg,
            score_threshold=self.score_threshold)
        for pick in picks:
            try:
                await self._enter(pick, ts)
            except Exception as exc:
                # Don't re-hammer a symbol the broker refused; one line, once.
                self.rejected.add(pick["symbol"])
                print(f"[bot] ENTRY REJECTED {pick['symbol']}: {exc}")

    def _journal_alert(self, ts, row, now, observed):
        setup = row.get("setup") or {}
        stop = setup.get("stop")
        r_dollars = ((row["price"] - stop) if stop and stop < row["price"]
                     else row["price"] * self.cfg.bot_stop_pct / 100)
        self.journal.record_alert(ts, row["symbol"], row["price"], r_dollars,
                                  features_from_row(row, now),
                                  setup=setup.get("setup"), observed=observed)

    async def _enter(self, pick, ts):
        entry = pick["price"]
        levels = exit_levels(entry, self.cfg, stop_price=pick.get("stop"))
        total_qty = pick["qty"]
        bank_qty, runner_qty = split_qty(total_qty)

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
            "entry": entry, "stop": levels["stop"],
            "scale_out": levels["scale_out"], "opened_ts": ts,
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
                exit_price = await self._closed_exit_price(symbol, trade)
                reason = "trailing" if trade["banked"] else "stop"
                self.journal.record_trade_close(trade["trade_id"], ts,
                                                exit_price, reason)
                del self.open_trades[symbol]
                print(f"[bot] CLOSED {symbol} @~{exit_price:.2f} ({reason})")
                continue

            latest = state.latest.get(symbol)
            price = (latest["price"] if latest
                     else float(pos.get("current_price") or trade["entry"]))

            if flatten:
                await self._flatten_trade(symbol, trade, ts, pos, "flatten")
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
        """Share-weighted average of all closed sell fills for the symbol."""
        orders = await self.broker._request(
            "GET", "/v2/orders",
            params={"status": "closed", "symbols": symbol,
                    "limit": 50, "nested": "true"})
        legs = []
        for order in orders or []:
            for leg in [order] + (order.get("legs") or []):
                if leg.get("side") == "sell" and leg.get("filled_avg_price"):
                    legs.append((float(leg.get("filled_qty") or 0),
                                 float(leg["filled_avg_price"])))
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
            "bankroll": self.cfg.bot_bankroll,
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
        print(f"[bot] paper account ok — equity ${float(account['equity']):,.0f} "
              f"(simulating ${cfg.bot_bankroll:,.0f} bankroll)")
        journal = Journal(cfg.bot_journal_path)
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
