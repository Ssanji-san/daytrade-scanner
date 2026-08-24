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

    async def submit_oto_stop(self, symbol, qty, stop_price, limit_price=None):
        return self._new(side="buy", type="market", order_class="oto",
                         symbol=symbol, qty=qty, stop_price=stop_price,
                         limit_price=limit_price)

    async def cancel_orders_for(self, symbol):
        for o in self.orders:
            if o.get("symbol") == symbol:
                self.cancelled.append(o["id"])

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


def test_enter_uses_one_atomic_order_not_two(tmp_path):
    """Buy + stop must go in a single OTO order.

    Two separate opposite-side orders are rejected by Alpaca as a wash
    trade, which previously blocked every entry.
    """
    bot, broker, journal = make_bot(tmp_path)
    asyncio.run(bot._enter(a_pick(price=5.0, qty=50), ts=1_700_000_000))

    assert len(broker.orders) == 1, "entry must be ONE order, not buy + stop"
    order = broker.orders[0]
    assert order["order_class"] == "oto"
    assert order["side"] == "buy" and order["qty"] == 50
    assert order["stop_price"] == pytest.approx(4.85)
    assert not any(o["side"] == "sell" for o in broker.orders)

    trade = bot.open_trades["HODX"]
    assert trade["bank_qty"] + trade["runner_qty"] == 50
    assert trade["scale_out"] == pytest.approx(5.30)   # 5.00 + 2 * 0.15
    assert trade["banked"] is False
    assert journal.trades_today("2023-11-14")  # record_trade_open persisted


def test_rejected_entry_is_not_retried_all_session(tmp_path):
    """A broker refusal must not re-fire every poll cycle."""
    import datetime as dt
    bot, broker, _ = make_bot(tmp_path)

    async def refuse(*a, **k):
        raise RuntimeError("422 wash trade")
    broker.submit_oto_stop = refuse

    class State:
        latest = {}
        def payload(self, now, require_news=None):
            row = {"symbol": "HODX", "price": 5.0, "rvol": 9.0, "day_pct": 22.0,
                   "float_shares": 8e6, "has_news": True, "dist_from_hod": 0.0,
                   "day_high": 5.0, "changes": {"5": 3.0}, "above_vwap": True,
                   "setup": {"setup": "micro_pullback", "stop": 4.85}}
            return {"hod": {"qualified": [row]}}

    now = et(10, 0)
    asyncio.run(bot.cycle(State(), now))
    assert bot.rejected == {"HODX"}
    asyncio.run(bot.cycle(State(), now + dt.timedelta(seconds=3)))
    assert bot.rejected == {"HODX"}      # still skipped, no second attempt


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

    assert broker.cancelled                                 # -1R stop pulled
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


def test_near_misses_are_learned_from_but_never_traded(tmp_path):
    """Near-miss rows grow the training set without loosening what it buys."""
    bot, broker, journal = make_bot(tmp_path)

    good = {"symbol": "GOOD", "price": 5.0, "rvol": 9.0, "day_pct": 22.0,
            "float_shares": 8e6, "has_news": True, "dist_from_hod": 0.0,
            "day_high": 5.0, "changes": {"5": 3.0}, "above_vwap": True,
            "setup": {"setup": "micro_pullback", "stop": 4.85}}
    near = dict(good, symbol="NEAR", failed=["rvol"])

    class State:
        latest = {}
        def payload(self, now, require_news=None):
            return {"hod": {"qualified": [good], "near": [near]}}

    asyncio.run(bot.cycle(State(), et(10, 0)))

    graded = {a["symbol"]: a["observed"] for a in journal.recent_alerts(10)}
    assert graded == {"GOOD": 0, "NEAR": 1}          # both journalled
    assert [t["symbol"] for t in journal.trades_today("2026-07-14")] == ["GOOD"]
    assert "NEAR" not in bot.open_trades              # never traded

    progress = journal.learning_progress(40)
    assert progress["labeled"] == 0                   # not resolved yet
    assert progress["needed"] == 40


def test_journal_failure_after_entry_unwinds_the_order(tmp_path):
    """A live order the journal does not know about is untracked risk."""
    bot, broker, journal = make_bot(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("attempt to write a readonly database")
    journal.record_trade_open = boom
    broker._positions = [{"symbol": "HODX", "current_price": 5.0}]

    with pytest.raises(RuntimeError):
        asyncio.run(bot._enter(a_pick(), ts=1_700_000_000))

    assert "HODX" not in bot.open_trades
    assert broker.cancelled                       # entry order pulled
    assert broker._positions == []                # position closed
