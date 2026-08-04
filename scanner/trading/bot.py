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
                       split_qty, weighted_exit, _parse_hhmm)

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
        "day_pct": row.get("day_pct") or 0.0,
        "float_shares": row.get("float_shares") or 0.0,
        "has_news": 1.0 if row.get("has_news") else 0.0,
        "dist_from_hod": row.get("dist_from_hod") or 0.0,
        "change_5": (row.get("changes") or {}).get("5")
                    or (row.get("changes") or {}).get(5) or 0.0,
        "minutes_since_open": _minutes_since_open(now),
    }


def choose_entries(qualified_rows, scorer, trades_today, traded_symbols,
                   day_pnl, now, cfg: Config):
    """Best-scored qualifying rows first, never exceeding the daily cap."""
    scored = []
    for row in qualified_rows:
        features = features_from_row(row, now)
        scored.append((scorer.score(features), row, features))
    scored.sort(key=lambda t: -t[0])

    picks, taken = [], set(traded_symbols)
    for score, row, features in scored:
        count = trades_today + len(picks)
        take, _ = should_enter(row["symbol"], price=row["price"], score=score,
                               trades_today=count, traded_symbols=taken,
                               day_pnl=day_pnl, now=now, cfg=cfg)
        if not take:
            continue
        qty, stop = size_position(row["price"], cfg)
        if qty < 1:
            continue
        picks.append({"symbol": row["symbol"], "price": row["price"],
                      "qty": qty, "stop": stop, "score": score,
                      "features": features})
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
        self.scorer, self.model_meta = self._retrain()
        self.error = None
        self.equity_history = None
        self._flattened_day = None

    def _retrain(self):
        dataset = self.journal.labeled_dataset()
        scorer, meta = train(dataset, min_samples=self.cfg.bot_model_min_samples)
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
            r_dollars = row["price"] * self.cfg.bot_stop_pct / 100
            self.journal.record_alert(ts, row["symbol"], row["price"],
                                      r_dollars, features_from_row(row, now))
        for alert_id, symbol in self.journal.open_alerts():
            latest = state.latest.get(symbol)
            if latest:
                self.journal.track_alert(alert_id, ts, latest["price"])

        # 2. manage open trades (fills, time stop, flatten)
        await self._manage_open(state, now, ts)

        # 3. new entries
        if _past(now, self.cfg.bot_flatten_time):
            return
        trades = self.journal.trades_today(day)
        picks = choose_entries(
            qualified, self.scorer,
            trades_today=len(trades),
            traded_symbols={t["symbol"] for t in trades},
            day_pnl=self.journal.day_pnl(day),
            now=now, cfg=self.cfg)
        for pick in picks:
            await self._enter(pick, ts)

    async def _enter(self, pick, ts):
        entry = pick["price"]
        levels = exit_levels(entry, self.cfg)
        total_qty = pick["qty"]
        bank_qty, runner_qty = split_qty(total_qty)

        await self.broker.submit_market_buy(pick["symbol"], total_qty)
        stop = await self.broker.submit_stop(
            pick["symbol"], total_qty, levels["stop"])

        trade_id = self.journal.record_trade_open(
            ts, pick["symbol"], qty=total_qty, entry=entry,
            stop=levels["stop"], targets=[levels["scale_out"]],
            features=pick["features"])
        self.open_trades[pick["symbol"]] = {
            "trade_id": trade_id, "stop_order_id": stop["id"],
            "trailing_order_id": None, "qty": total_qty,
            "bank_qty": bank_qty, "runner_qty": runner_qty,
            "entry": entry, "stop": levels["stop"],
            "scale_out": levels["scale_out"], "opened_ts": ts,
            "banked": False}
        print(f"[bot] ENTER {pick['symbol']} x{total_qty} @~{entry:.2f} "
              f"stop {levels['stop']:.2f} scale-out {levels['scale_out']:.2f}")

    async def _manage_open(self, state, now, ts):
        if not self.open_trades:
            # flatten leftovers from crashes exactly once per day after cutoff
            return
        positions = {p["symbol"]: p for p in await self.broker.positions()}
        flatten = _past(now, self.cfg.bot_flatten_time)
        for symbol, trade in list(self.open_trades.items()):
            pos = positions.get(symbol)
            if pos is None:
                # brackets did their job; find the exit fills
                exit_price = await self._closed_exit_price(symbol, trade)
                self.journal.record_trade_close(trade["trade_id"], ts,
                                                exit_price, "bracket")
                del self.open_trades[symbol]
                print(f"[bot] CLOSED {symbol} @~{exit_price:.2f} (bracket)")
                continue
            age_min = (ts - trade["opened_ts"]) / 60
            if flatten or age_min >= self.cfg.bot_time_stop_minutes:
                reason = "flatten" if flatten else "time_stop"
                for order_id in trade["order_ids"]:
                    try:
                        await self.broker.cancel_order(order_id)
                    except aiohttp.ClientResponseError:
                        pass   # already filled/cancelled
                await self.broker.close_position(symbol)
                exit_price = float(pos.get("current_price")
                                   or trade["entry"])
                self.journal.record_trade_close(trade["trade_id"], ts,
                                                exit_price, reason)
                del self.open_trades[symbol]
                print(f"[bot] CLOSED {symbol} @~{exit_price:.2f} ({reason})")

    async def _closed_exit_price(self, symbol, trade):
        """Weighted average sell fill across the trade's bracket legs."""
        total_qty = total_value = 0.0
        for order_id in trade["order_ids"]:
            try:
                order = await self.broker.order(order_id)
            except aiohttp.ClientResponseError:
                continue
            for leg in (order.get("legs") or []):
                if leg.get("side") == "sell" and leg.get("filled_avg_price"):
                    qty = float(leg.get("filled_qty") or 0)
                    total_qty += qty
                    total_value += qty * float(leg["filled_avg_price"])
        if total_qty:
            return total_value / total_qty
        return trade["entry"]

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
            "recent": self.journal.recent_trades(20),
            "stats": self.journal.rolling_stats(20),
            "model": {k: v for k, v in self.model_meta.items()
                      if k != "weights"},
            "model_history": self.journal.model_history(10),
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
                    last_equity_pull = now.timestamp()
                bot.error = None
            except Exception as exc:
                bot.error = str(exc)
                traceback.print_exc()
            day = now.astimezone(ET).strftime("%Y-%m-%d")
            ctx["bot_status"] = bot.status(day)
            await asyncio.sleep(cfg.poll_seconds)
