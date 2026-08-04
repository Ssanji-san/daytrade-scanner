"""Async orchestration tests for the trading bot (fake broker, real journal)."""
import asyncio
import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.trading.bot import TradingBot
from scanner.trading.journal import Journal

ET = ZoneInfo("America/New_York")
CFG = Config()


def et(hour, minute):
    return dt.datetime(2026, 7, 14, hour, minute, tzinfo=ET)


class FakeBroker:
    def __init__(self):
        self.orders = []          # submitted payload-ish dicts (with id)
        self.cancelled = []
        self._positions = []      # list of {"symbol","current_price"}
        self.closed_orders = []   # returned for the closed-orders query
        self._id = 0

    def _new(self, **kw):
        self._id += 1
        kw["id"] = f"o{self._id}"
        self.orders.append(kw)
        return {"id": kw["id"]}

    async def account(self):
        return {"equity": "100000"}

    async def positions(self):
        return list(self._positions)

    async def submit_market_buy(self, symbol, qty):
        return self._new(side="buy", type="market", symbol=symbol, qty=qty)

    async def submit_market_sell(self, symbol, qty):
        return self._new(side="sell", type="market", symbol=symbol, qty=qty)

    async def submit_stop(self, symbol, qty, stop_price):
        return self._new(side="sell", type="stop", symbol=symbol, qty=qty,
                         stop_price=stop_price)

    async def submit_trailing_stop(self, symbol, qty, trail_percent):
        return self._new(side="sell", type="trailing_stop", symbol=symbol,
                         qty=qty, trail_percent=trail_percent)

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    async def close_position(self, symbol):
        self._positions = [p for p in self._positions if p["symbol"] != symbol]

    async def portfolio_history(self, *a, **k):
        return {"timestamp": [], "equity": []}

    async def _request(self, method, path, params=None, json=None):
        if params and params.get("status") == "closed":
            return self.closed_orders
        return None


class FakeState:
    def __init__(self, latest):
        self.latest = latest


def make_bot(tmp_path, **cfg_overrides):
    cfg = replace(CFG, **cfg_overrides) if cfg_overrides else CFG
    journal = Journal(str(tmp_path / "j.db"))
    broker = FakeBroker()
    return TradingBot(cfg, journal, broker), broker, journal


def a_pick(price=5.0, qty=50):
    return {"symbol": "HODX", "price": price, "qty": qty, "stop": 4.85,
            "score": 0.8, "features": {"rvol": 8.0}}


def test_enter_places_buy_and_full_size_stop(tmp_path):
    bot, broker, journal = make_bot(tmp_path)
    asyncio.run(bot._enter(a_pick(price=5.0, qty=50), ts=1_700_000_000))

    kinds = {(o["side"], o["type"]) for o in broker.orders}
    assert ("buy", "market") in kinds
    assert ("sell", "stop") in kinds
    stop = next(o for o in broker.orders if o["type"] == "stop")
    assert stop["qty"] == 50 and stop["stop_price"] == pytest.approx(4.85)

    trade = bot.open_trades["HODX"]
    assert trade["bank_qty"] + trade["runner_qty"] == 50
    assert trade["scale_out"] == pytest.approx(5.30)   # 5.00 + 2 * 0.15
    assert trade["banked"] is False
    assert journal.trades_today("2023-11-14")  # record_trade_open persisted


def _open_a_trade(bot, ts, price=5.0, qty=50):
    asyncio.run(bot._enter(a_pick(price=price, qty=qty), ts=ts))
    return bot.open_trades["HODX"]


def test_scale_out_banks_half_and_starts_trailing(tmp_path):
    bot, broker, _ = make_bot(tmp_path)
    trade = _open_a_trade(bot, ts=int(et(10, 0).timestamp()))
    broker._positions = [{"symbol": "HODX", "current_price": 5.30}]
    state = FakeState({"HODX": {"price": 5.30}})            # at +2R

    asyncio.run(bot._manage_open(state, now=et(10, 5),
                                 ts=int(et(10, 5).timestamp())))

    assert trade["stop_order_id"] in broker.cancelled       # -1R stop pulled
    sells = [o for o in broker.orders if o["type"] == "market" and o["side"] == "sell"]
    assert sells and sells[0]["qty"] == trade["bank_qty"]    # banked half
    trail = [o for o in broker.orders if o["type"] == "trailing_stop"]
    assert trail and trail[0]["qty"] == trade["runner_qty"]
    assert trade["banked"] is True


def test_time_stop_cuts_a_stalled_trade_before_scale_out(tmp_path):
    bot, broker, journal = make_bot(tmp_path)
    open_ts = int(et(10, 0).timestamp())
    _open_a_trade(bot, ts=open_ts)
    broker._positions = [{"symbol": "HODX", "current_price": 5.05}]  # below +2R
    state = FakeState({"HODX": {"price": 5.05}})
    late = et(10, 21)                                        # 21 min later

    asyncio.run(bot._manage_open(state, now=late, ts=int(late.timestamp())))

    assert "HODX" not in bot.open_trades
    closed = journal.recent_trades(1)[0]
    assert closed["exit_reason"] == "time_stop"


def test_runner_stays_open_past_time_stop(tmp_path):
    bot, broker, _ = make_bot(tmp_path)
    open_ts = int(et(10, 0).timestamp())
    trade = _open_a_trade(bot, ts=open_ts)
    trade["banked"] = True                                  # already a runner
    broker._positions = [{"symbol": "HODX", "current_price": 6.0}]
    state = FakeState({"HODX": {"price": 6.0}})
    late = et(10, 40)

    asyncio.run(bot._manage_open(state, now=late, ts=int(late.timestamp())))

    assert "HODX" in bot.open_trades                        # not time-stopped


def test_close_records_blended_r_from_sell_fills(tmp_path):
    bot, broker, journal = make_bot(tmp_path)
    _open_a_trade(bot, ts=int(et(10, 0).timestamp()))
    bot.open_trades["HODX"]["banked"] = True
    broker._positions = []                                  # fully closed
    broker.closed_orders = [
        {"side": "sell", "filled_qty": "25", "filled_avg_price": "5.30", "legs": []},
        {"side": "sell", "filled_qty": "25", "filled_avg_price": "5.60", "legs": []},
    ]
    state = FakeState({})

    asyncio.run(bot._manage_open(state, now=et(11, 0),
                                 ts=int(et(11, 0).timestamp())))

    trade = journal.recent_trades(1)[0]
    assert trade["exit_price"] == pytest.approx(5.45)       # weighted average
    assert trade["r_multiple"] == pytest.approx(3.0)        # (5.45-5.0)/0.15
