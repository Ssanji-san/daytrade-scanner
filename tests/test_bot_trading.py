"""Async orchestration tests for the trading bot (fake broker, real journal)."""
import asyncio
import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scanner.config import Config
from scanner.trading.bot import TradingBot
from scanner.trading.broker import Broker
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
        # What GET /v2/orders/{id} says about the entry. Tests that care
        # about an entry still working set this to "new".
        self.order_status = "filled"
        self.order_fill_price = None
        self.equity = "100000"
        self.buying_power = None
        self._id = 0

    def _new(self, **kw):
        self._id += 1
        kw["id"] = f"o{self._id}"
        self.orders.append(kw)
        return {"id": kw["id"]}

    async def account(self):
        account = {"equity": self.equity}
        if self.buying_power is not None:
            account["buying_power"] = self.buying_power
        return account

    async def positions(self):
        return list(self._positions)

    async def submit_market_sell(self, symbol, qty):
        return self._new(side="sell", type="market", symbol=symbol, qty=qty)

    async def submit_stop(self, symbol, qty, stop_price):
        return self._new(side="sell", type="stop", symbol=symbol, qty=qty,
                         stop_price=stop_price)

    async def submit_limit_buy(self, symbol, qty, limit_price,
                               extended_hours=False):
        return self._new(side="buy", type="limit", symbol=symbol, qty=qty,
                         limit_price=limit_price, extended_hours=extended_hours)

    async def submit_limit_sell(self, symbol, qty, limit_price,
                                extended_hours=False):
        return self._new(side="sell", type="limit", symbol=symbol, qty=qty,
                         limit_price=limit_price, extended_hours=extended_hours)

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

    async def order(self, order_id):
        return {"id": order_id, "status": self.order_status,
                "filled_avg_price": self.order_fill_price}

    async def closed_sell_legs(self, symbol, after_ts=None):
        return Broker.sell_legs(self.closed_orders, after_ts)

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
    assert order["stop_price"] == pytest.approx(4.75)   # flat 5% scalp stop
    assert not any(o["side"] == "sell" for o in broker.orders)

    trade = bot.open_trades["HODX"]
    assert trade["bank_qty"] + trade["runner_qty"] == 50
    assert trade["scale_out"] == pytest.approx(5.20)   # 5.00 + 20c
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
    # The swing path - scale at +2R, trail the runner. Still reachable by
    # configuration, though the live default now scalps.
    bot, broker, _ = make_bot(tmp_path, bot_scalp_mode=False,
                              bot_stop_pct=3.0, bot_min_stop_pct=1.0,
                              bot_max_stop_pct=6.0)
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
    late = et(14, 5)                                     # 4h05m later

    asyncio.run(bot._manage_open(state, now=late, ts=int(late.timestamp())))

    assert "HODX" not in bot.open_trades
    closed = journal.recent_trades(1)[0]
    assert closed["exit_reason"] == "time_stop"


def test_runner_stays_open_past_time_stop(tmp_path):
    bot, broker, _ = make_bot(tmp_path, bot_scalp_mode=False,
                              bot_time_stop_minutes=20)
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
    assert trade["r_multiple"] == pytest.approx(1.8)        # (5.45-5.0)/0.25


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


def test_before_the_window_opens_is_observed_but_never_traded(tmp_path):
    """Starting the session at 07:30 must not start trading at 07:30.

    The window opens at 08:00, when IEX's pre-market session does; before
    that there is no IEX print to trade against. The alert journal still
    records what it sees. (Pre-market entries from 08:00 go through their
    own extended-hours path - see TestPremarketExecution.)
    """
    bot, broker, journal = make_bot(tmp_path)
    row = {"symbol": "GAPR", "price": 5.0, "rvol": 30.0, "day_pct": 180.0,
           "float_shares": 8e6, "has_news": True, "dist_from_hod": 0.0,
           "day_high": 5.0, "changes": {"5": 20.0}, "above_vwap": True,
           "gap_pct": 180.0,
           "setup": {"setup": "opening_range", "stop": 4.85}}

    class State:
        latest = {}
        def payload(self, now, require_news=None):
            return {"hod": {"qualified": [row], "near": []}}

    asyncio.run(bot.cycle(State(), et(7, 45)))       # premarket

    assert broker.orders == []                        # nothing traded
    assert bot.open_trades == {}
    assert [a["symbol"] for a in journal.recent_alerts(5)] == ["GAPR"]


# --------------------------------------------------------------- scalping
# The live exits have to match scanner.backtest.simulate, or the bot trades
# a strategy the backtest never measured. These pin the shared behaviour.

def _doji(t, price=5.10):
    return {"t": t, "o": price, "c": price + 0.002,
            "h": price + 0.05, "l": price - 0.05, "v": 1000}


def _drive(t, price=5.10):
    return {"t": t, "o": price, "c": price + 0.08,
            "h": price + 0.09, "l": price - 0.01, "v": 1000}


class _BarState(FakeState):
    """FakeState plus the completed-bar history the stall exit reads."""

    def __init__(self, latest, bars):
        super().__init__(latest)
        class _H:
            def __init__(self, b): self.completed_bars = b
        self.histories = {"HODX": _H(bars)}


def test_scalp_banks_the_majority_and_trails_the_runner(tmp_path):
    """65% off at the target, the rest rides a stop that ratchets up.

    The runner used to sit behind a fixed stop at the entry, so every cent
    it made above +20c was handed back the moment price came off.
    """
    bot, broker, _ = make_bot(tmp_path)
    trade = _open_a_trade(bot, ts=int(et(9, 40).timestamp()))
    assert (trade["bank_qty"], trade["runner_qty"]) == (32, 18)   # 65 / 35
    broker._positions = [{"symbol": "HODX", "current_price": 5.22}]
    state = FakeState({"HODX": {"price": 5.22}})                  # past +20c

    asyncio.run(bot._manage_open(state, now=et(9, 42),
                                 ts=int(et(9, 42).timestamp())))

    sells = [o for o in broker.orders
             if o["type"] == "market" and o["side"] == "sell"]
    assert sells and sells[0]["qty"] == 32
    trails = [o for o in broker.orders if o["type"] == "trailing_stop"]
    assert trails and trails[0]["qty"] == 18
    # Capped: a flat 5% of $5.22 would put the first stop at $4.96, under the
    # $5.00 paid. 4.21% of 5.22 lands it exactly on the entry.
    assert trails[0]["trail_percent"] == pytest.approx(4.21)
    assert 5.22 * (1 - trails[0]["trail_percent"] / 100) >= trade["entry"]
    assert not [o for o in broker.orders if o["type"] == "stop"]
    assert trade["stop"] == pytest.approx(5.00)     # the floor is recorded
    assert "HODX" in bot.open_trades                # runner still on


def test_a_cheap_runner_gets_the_full_trail_width(tmp_path):
    """Down at $2 the full 5% already sits above break-even, so it is used."""
    bot, broker, _ = make_bot(tmp_path)
    _open_a_trade(bot, ts=int(et(9, 40).timestamp()), price=2.00, qty=500)
    broker._positions = [{"symbol": "HODX", "current_price": 2.22}]

    asyncio.run(bot._manage_open(FakeState({"HODX": {"price": 2.22}}),
                                 now=et(9, 42), ts=int(et(9, 42).timestamp())))

    trails = [o for o in broker.orders if o["type"] == "trailing_stop"]
    assert trails and trails[0]["trail_percent"] == pytest.approx(5.0)


def test_a_banked_runner_outlives_the_time_stop(tmp_path):
    """The clock is for a position that has not paid yet."""
    bot, broker, _ = make_bot(tmp_path)
    open_ts = int(et(9, 40).timestamp())
    trade = _open_a_trade(bot, ts=open_ts)
    trade["banked"] = True
    broker._positions = [{"symbol": "HODX", "current_price": 5.40}]
    late = et(9, 40) + dt.timedelta(minutes=CFG.bot_time_stop_minutes + 5)

    asyncio.run(bot._manage_open(FakeState({"HODX": {"price": 5.40}}),
                                 now=late, ts=int(late.timestamp())))

    assert "HODX" in bot.open_trades


def test_the_fixed_break_even_stop_is_still_reachable(tmp_path):
    bot, broker, _ = make_bot(tmp_path, bot_scalp_runner_trail=False)
    trade = _open_a_trade(bot, ts=int(et(9, 40).timestamp()))
    broker._positions = [{"symbol": "HODX", "current_price": 5.22}]

    asyncio.run(bot._manage_open(FakeState({"HODX": {"price": 5.22}}),
                                 now=et(9, 42), ts=int(et(9, 42).timestamp())))

    stops = [o for o in broker.orders if o["type"] == "stop"]
    assert stops and stops[0]["stop_price"] == pytest.approx(5.00)
    assert not [o for o in broker.orders if o["type"] == "trailing_stop"]


def test_two_dojis_close_the_scalp(tmp_path):
    bot, broker, journal = make_bot(tmp_path)
    open_ts = int(et(9, 40).timestamp())
    _open_a_trade(bot, ts=open_ts)
    broker._positions = [{"symbol": "HODX", "current_price": 5.05}]
    bars = [_doji("2026-07-14T13:42:00Z"), _doji("2026-07-14T13:43:00Z")]
    state = _BarState({"HODX": {"price": 5.05}}, bars)

    asyncio.run(bot._manage_open(state, now=et(9, 44),
                                 ts=int(et(9, 44).timestamp())))

    assert "HODX" not in bot.open_trades
    assert journal.recent_trades(1)[0]["exit_reason"] == "stall"


def test_a_decisive_candle_keeps_the_scalp_open(tmp_path):
    bot, broker, _ = make_bot(tmp_path)
    _open_a_trade(bot, ts=int(et(9, 40).timestamp()))
    broker._positions = [{"symbol": "HODX", "current_price": 5.05}]
    bars = [_doji("2026-07-14T13:42:00Z"), _drive("2026-07-14T13:43:00Z")]
    state = _BarState({"HODX": {"price": 5.05}}, bars)

    asyncio.run(bot._manage_open(state, now=et(9, 44),
                                 ts=int(et(9, 44).timestamp())))
    assert "HODX" in bot.open_trades


def test_bars_from_before_the_entry_do_not_stall_it(tmp_path):
    """Otherwise a stock that was quiet before the breakout exits at once."""
    bot, broker, _ = make_bot(tmp_path)
    _open_a_trade(bot, ts=int(et(9, 45).timestamp()))
    broker._positions = [{"symbol": "HODX", "current_price": 5.05}]
    stale = [_doji("2026-07-14T13:31:00Z"), _doji("2026-07-14T13:32:00Z")]
    state = _BarState({"HODX": {"price": 5.05}}, stale)

    asyncio.run(bot._manage_open(state, now=et(9, 46),
                                 ts=int(et(9, 46).timestamp())))
    assert "HODX" in bot.open_trades


def test_the_scalp_time_stop_is_ten_minutes(tmp_path):
    bot, broker, journal = make_bot(tmp_path)
    _open_a_trade(bot, ts=int(et(9, 40).timestamp()))
    broker._positions = [{"symbol": "HODX", "current_price": 5.05}]
    state = FakeState({"HODX": {"price": 5.05}})
    late = et(9, 40) + dt.timedelta(minutes=CFG.bot_time_stop_minutes)

    asyncio.run(bot._manage_open(state, now=late, ts=int(late.timestamp())))
    assert journal.recent_trades(1)[0]["exit_reason"] == "time_stop"


class TestPendingEntries:
    """An accepted order is not a filled one.

    _enter registers the trade the moment the OTO order is accepted and the
    next cycle runs three seconds later, so a marketable limit on a thin
    name has often not filled yet. Treating "no position" as "the trade
    closed" journalled a phantom exit at the entry price - the live journal
    holds an IVF trade open for exactly one poll cycle, exited at its own
    entry for 0R - and then forgot an order that could still fill, leaving a
    position with no scale-out, no time stop and no stall exit.
    """

    def _pending(self, tmp_path, status="new"):
        bot, broker, journal = make_bot(tmp_path)
        broker.order_status = status
        open_ts = int(et(9, 40).timestamp())
        asyncio.run(bot._enter(a_pick(), ts=open_ts))
        broker._positions = []                    # nothing filled yet
        return bot, broker, journal, open_ts

    def _manage(self, bot, at):
        asyncio.run(bot._manage_open(FakeState({}), now=at,
                                     ts=int(at.timestamp())))

    def test_an_unfilled_entry_is_not_journalled_as_closed(self, tmp_path):
        bot, broker, journal, _ = self._pending(tmp_path)
        self._manage(bot, et(9, 40) + dt.timedelta(seconds=3))
        assert "HODX" in bot.open_trades          # still working
        assert journal.recent_trades(5) == []     # nothing closed
        assert bot.open_trades["HODX"]["filled"] is False

    def test_a_fill_is_adopted_at_the_price_actually_paid(self, tmp_path):
        bot, broker, journal, _ = self._pending(tmp_path)
        broker.order_status = "filled"
        broker.order_fill_price = "5.08"          # 8c of slippage
        broker._positions = [{"symbol": "HODX", "current_price": 5.08,
                              "avg_entry_price": "5.08"}]

        self._manage(bot, et(9, 41))

        trade = bot.open_trades["HODX"]
        assert trade["filled"] is True
        assert trade["entry"] == pytest.approx(5.08)
        assert trade["signal_price"] == pytest.approx(5.00)
        assert journal.trades_today("2026-07-14")[0]["entry"] == pytest.approx(5.08)

    def test_a_rejected_entry_leaves_no_trade_behind(self, tmp_path):
        bot, broker, journal, _ = self._pending(tmp_path, status="rejected")
        self._manage(bot, et(9, 40) + dt.timedelta(seconds=3))
        assert bot.open_trades == {}
        assert journal.trades_today("2026-07-14") == []   # never happened

    def test_an_entry_that_never_fills_is_cancelled(self, tmp_path):
        bot, broker, journal, open_ts = self._pending(tmp_path)
        late = et(9, 40) + dt.timedelta(
            seconds=CFG.bot_entry_timeout_seconds + 1)

        self._manage(bot, late)

        assert bot.open_trades == {}
        assert broker.cancelled                   # the order was pulled
        assert journal.trades_today("2026-07-14") == []

    def _as_a_cycle_records_it(self, journal, open_ts):
        """The alert and the "taken" decision a real cycle writes first."""
        journal.record_alert(open_ts, "HODX", 5.00, 0.25, {"rvol": 8.0},
                             setup="micro_pullback")
        journal.record_decision(open_ts, "HODX", "taken")

    def _decision(self, journal):
        return journal._execute(
            "SELECT decision FROM alerts WHERE symbol='HODX'").fetchone()[0]

    def test_an_entry_that_never_fills_is_recorded_unfilled(self, tmp_path):
        """"taken" meant the broker accepted it. It never filled, so it was
        never taken - and until this, the journal could not tell."""
        bot, broker, journal, open_ts = self._pending(tmp_path)
        self._as_a_cycle_records_it(journal, open_ts)
        late = et(9, 40) + dt.timedelta(
            seconds=CFG.bot_entry_timeout_seconds + 1)

        self._manage(bot, late)

        assert self._decision(journal) == "unfilled"

    def test_a_rejected_entry_is_recorded_unfilled(self, tmp_path):
        bot, broker, journal, open_ts = self._pending(tmp_path,
                                                      status="rejected")
        self._as_a_cycle_records_it(journal, open_ts)
        self._manage(bot, et(9, 40) + dt.timedelta(seconds=3))
        assert self._decision(journal) == "unfilled"

    def test_a_filled_entry_stays_taken(self, tmp_path):
        bot, broker, journal, open_ts = self._pending(tmp_path)
        self._as_a_cycle_records_it(journal, open_ts)
        broker.order_status = "filled"
        broker.order_fill_price = "5.01"
        broker._positions = [{"symbol": "HODX", "current_price": 5.01,
                              "avg_entry_price": "5.01"}]
        self._manage(bot, et(9, 41))
        assert self._decision(journal) == "taken"

    def test_a_fill_with_no_position_still_records_the_close(self, tmp_path):
        """Bought and stopped out between two polls - that is a real trade."""
        bot, broker, journal, _ = self._pending(tmp_path)
        broker.order_status = "filled"
        broker.order_fill_price = "5.00"
        broker.closed_orders = [{"side": "sell", "filled_qty": "50",
                                 "filled_avg_price": "4.75", "legs": []}]

        self._manage(bot, et(9, 41))

        assert bot.open_trades == {}
        closed = journal.recent_trades(1)[0]
        assert closed["exit_price"] == pytest.approx(4.75)


class TestCapitalIsSpentInUnits:
    """A balance holds several $1,000 positions, not one trade for the lot.

    $2,473.74 used to go into a single position risking $123.69, with
    bot_max_concurrent_positions=1 blocking anything else. It now opens
    $1,000 + $1,000 + $473 and stops when the cash runs out.
    """

    def _rows(self, n, price=3.00):
        return [{"symbol": f"S{i}", "price": price, "rvol": 9.0,
                 "day_pct": 22.0, "float_shares": 8e6, "has_news": True,
                 "dist_from_hod": 0.0, "day_high": price,
                 "changes": {"5": 3.0}, "above_vwap": True,
                 "setup": {"setup": "micro_pullback",
                           "stop": round(price * 0.95, 2)}}
                for i in range(n)]

    def _state(self, rows):
        class State:
            latest = {}

            def payload(self, now, require_news=None):
                return {"hod": {"qualified": rows, "near": []}}
        return State()

    def test_one_cycle_fills_the_account_in_units(self, tmp_path):
        bot, broker, _ = make_bot(tmp_path)
        broker.equity = "2473.74"

        asyncio.run(bot.cycle(self._state(self._rows(4)), et(10, 0)))

        notionals = sorted((t["qty"] * t["entry"]
                            for t in bot.open_trades.values()), reverse=True)
        assert len(notionals) == 3          # the fourth had no capital left
        assert notionals[0] == pytest.approx(999.0, abs=3)
        assert notionals[1] == pytest.approx(999.0, abs=3)
        assert notionals[2] == pytest.approx(474.0, abs=3)
        assert sum(notionals) <= 2473.74

    def test_risk_per_position_stays_50_dollars(self, tmp_path):
        bot, broker, _ = make_bot(tmp_path)
        broker.equity = "2473.74"
        asyncio.run(bot.cycle(self._state(self._rows(2)), et(10, 0)))
        for trade in bot.open_trades.values():
            risk = (trade["entry"] - trade["stop"]) * trade["qty"]
            assert risk == pytest.approx(50.0, abs=1.0)

    def test_a_thousand_dollar_account_still_gets_one_trade(self, tmp_path):
        bot, broker, _ = make_bot(tmp_path)
        broker.equity = "1000.00"
        asyncio.run(bot.cycle(self._state(self._rows(3)), et(10, 0)))
        assert len(bot.open_trades) == 1

    def test_open_positions_are_subtracted_from_the_budget(self, tmp_path):
        bot, _, _ = make_bot(tmp_path)
        bot.bankroll = 2_473.74
        bot.open_trades["OLD"] = {"qty": 333, "entry": 3.00}
        assert bot._budget(None) == pytest.approx(1_474.74)

    def test_margin_buying_power_does_not_inflate_the_budget(self, tmp_path):
        """A paper account reports 4x its balance. We spend our own money."""
        bot, _, _ = make_bot(tmp_path)
        bot.bankroll = 2_473.74
        budget = bot._budget({"equity": "2473.74",
                              "buying_power": "9894.96"})
        assert budget == pytest.approx(2_473.74)

    def test_a_tighter_broker_limit_wins(self, tmp_path):
        bot, _, _ = make_bot(tmp_path)
        bot.bankroll = 2_473.74
        budget = bot._budget({"equity": "2473.74", "buying_power": "800"})
        assert budget == pytest.approx(800.0)

    def test_the_first_account_reading_is_the_baseline(self, tmp_path):
        """The 3x guard must not measure the first read against the seed."""
        bot, broker, _ = make_bot(tmp_path)
        broker.equity = "4000.00"
        asyncio.run(bot.cycle(self._state([]), et(10, 0)))
        assert bot.bankroll == pytest.approx(4_000.0)
        assert bot.status("2026-07-14")["slots"] == 4



# ------------------------------------------------------ pre-market execution

class TestPremarketEntry:
    """08:00-09:30: Ross's pre-market entry, and nothing Alpaca will refuse.

    Outside regular hours Alpaca takes only extended-hours limit orders.
    The entry is a plain limit at the ask plus the offset - no OTO, so no
    stop rides along; the bot runs it (TestPremarketStop)."""

    def _rows(self, price=2.00, ask=2.03):
        return [{"symbol": "PRE", "price": price, "ask": ask, "bid": price - 0.02,
                 "rvol": 9.0, "day_pct": 30.0, "float_shares": 8e6,
                 "has_news": True, "dist_from_hod": 0.0, "day_high": price,
                 "changes": {"5": 3.0}, "above_vwap": True,
                 # 3% under: inside the 5% band, so technical_stop clamps it
                 # to exactly 5%. A setup stop AT 5% sits on a floating-point
                 # knife edge and can read as too wide.
                 "setup": {"setup": "micro_pullback",
                           "stop": round(price * 0.97, 2)}}]

    def _state(self, rows):
        class State:
            latest = {}

            def payload(self, now, require_news=None):
                return {"hod": {"qualified": rows, "near": []}}
        return State()

    def _cycle(self, tmp_path, at, **rows):
        bot, broker, journal = make_bot(tmp_path)
        broker.equity = "2473.74"
        asyncio.run(bot.cycle(self._state(self._rows(**rows)), at))
        return bot, broker

    def test_it_is_an_extended_hours_limit_at_the_ask_plus_10c(self, tmp_path):
        bot, broker = self._cycle(tmp_path, et(8, 45))
        buys = [o for o in broker.orders if o["side"] == "buy"]
        assert len(buys) == 1
        assert buys[0]["type"] == "limit" and buys[0]["extended_hours"] is True
        assert buys[0]["limit_price"] == pytest.approx(2.13)   # ask 2.03 + 10c
        assert "order_class" not in buys[0]                     # no OTO
        assert bot.open_trades["PRE"]["managed_stop"] is True

    def test_it_is_sized_so_a_full_fill_still_risks_50(self, tmp_path):
        bot, broker = self._cycle(tmp_path, et(8, 45))
        trade = bot.open_trades["PRE"]
        assert trade["qty"] * (trade["limit"] - 2.00 * 0.95) == pytest.approx(50, abs=1)

    def test_no_quote_falls_back_to_the_last_trade(self, tmp_path):
        bot, broker = self._cycle(tmp_path, et(8, 45), ask=None)
        buy = [o for o in broker.orders if o["side"] == "buy"][0]
        assert buy["limit_price"] == pytest.approx(2.10)

    def test_after_the_bell_the_entry_is_the_old_oto_unchanged(self, tmp_path):
        """09:30-10:00 keeps today's path exactly: OTO, 0.3%, broker stop."""
        bot, broker = self._cycle(tmp_path, et(9, 45))
        buy = [o for o in broker.orders if o["side"] == "buy"][0]
        assert buy["order_class"] == "oto"
        assert buy["limit_price"] == pytest.approx(2.00 * 1.003)
        assert "extended_hours" not in buy
        assert bot.open_trades["PRE"]["managed_stop"] is False


class _Hist:
    """Just what _fresh and _stalled read: when the price last came in, and
    completed bars (none, so nothing reads as a stall)."""

    def __init__(self, at, price):
        self.latest = (at, price, 1_000_000)
        self.completed_bars = []


class PremarketState:
    def __init__(self, at, price, bid=None, fresh=True):
        seen = at if fresh else at - dt.timedelta(
            seconds=CFG.bot_stale_quote_seconds + 1)
        self.latest = {"PRE": {"price": price, "bid": bid}}
        self.histories = {"PRE": _Hist(seen, price)}


def _at(hour, minute, second=0):
    return et(hour, minute) + dt.timedelta(seconds=second)


class TestPremarketStop:
    """Before 09:30 the broker holds no stop for us - the bot runs it.

    Alpaca refuses stop, trailing and market orders outside regular hours,
    so a pre-market position is entered with no stop attached. If anything
    here is wrong, a position has no stop at all. Every behaviour is pinned.
    """

    OPEN = (8, 40)

    def _open(self, tmp_path):
        bot, broker, journal = make_bot(tmp_path)
        pick = {"symbol": "PRE", "price": 2.00, "premarket": True,
                "limit": 2.13, "qty": 200, "stop": 1.90, "score": 0.9,
                "setup": "micro_pullback", "features": {"rvol": 9.0}}
        asyncio.run(bot._enter(pick, ts=int(et(*self.OPEN).timestamp())))
        broker._positions = [{"symbol": "PRE", "qty": "200",
                              "avg_entry_price": "2.00",
                              "current_price": 2.00}]
        return bot, broker, journal

    def _manage(self, bot, state, at):
        asyncio.run(bot._manage_open(state, now=at, ts=int(at.timestamp())))

    def _sells(self, broker):
        return [o for o in broker.orders if o["side"] == "sell"]

    def test_the_stop_fires_as_an_extended_hours_limit(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        at = _at(8, 41)
        self._manage(bot, PremarketState(at, 1.89, bid=1.88), at)

        sell = self._sells(broker)[-1]
        assert sell["type"] == "limit" and sell["extended_hours"] is True
        assert sell["qty"] == 200
        assert sell["limit_price"] == pytest.approx(1.78)   # bid 1.88 - 10c
        assert bot.open_trades["PRE"]["exit"]["reason"] == "stop"

    def test_the_close_is_not_recorded_until_the_position_is_gone(self, tmp_path):
        """A limit may not fill. Recording it straight away - as the market
        flatten does - would journal a close that has not happened."""
        bot, broker, journal = self._open(tmp_path)
        at = _at(8, 41)
        self._manage(bot, PremarketState(at, 1.89, bid=1.88), at)
        assert journal.recent_trades(5) == []
        assert "PRE" in bot.open_trades

        broker._positions = []                         # the sell filled
        broker.closed_orders = [{"side": "sell", "filled_qty": "200",
                                 "filled_avg_price": "1.87", "legs": []}]
        later = _at(8, 41, 3)
        self._manage(bot, PremarketState(later, 1.87, bid=1.86), later)
        closed = journal.recent_trades(1)[0]
        assert closed["exit_reason"] == "stop"
        assert closed["exit_price"] == pytest.approx(1.87)
        assert "PRE" not in bot.open_trades

    def test_an_unfilled_stop_is_repriced_lower(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        at = _at(8, 41)
        self._manage(bot, PremarketState(at, 1.89, bid=1.88), at)
        first = self._sells(broker)[-1]["limit_price"]

        later = at + dt.timedelta(seconds=CFG.bot_premarket_chase_seconds)
        self._manage(bot, PremarketState(later, 1.89, bid=1.88), later)
        second = self._sells(broker)[-1]
        assert broker.cancelled                         # the old one pulled
        assert second["limit_price"] == pytest.approx(first - 0.10)
        assert second["extended_hours"] is True

    def test_it_is_not_repriced_before_the_chase_interval(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        at = _at(8, 41)
        self._manage(bot, PremarketState(at, 1.89, bid=1.88), at)
        n = len(self._sells(broker))
        soon = at + dt.timedelta(seconds=CFG.bot_premarket_chase_seconds - 2)
        self._manage(bot, PremarketState(soon, 1.89, bid=1.88), soon)
        assert len(self._sells(broker)) == n

    def test_a_stale_price_closes_the_position(self, tmp_path):
        """The stop is only as live as the price it watches."""
        bot, broker, journal = self._open(tmp_path)
        at = _at(8, 45)
        self._manage(bot, PremarketState(at, 2.05, fresh=False), at)
        assert bot.open_trades["PRE"]["exit"]["reason"] == "stale"
        assert self._sells(broker)[-1]["extended_hours"] is True

    def test_holding_above_the_stop_sends_nothing(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        at = _at(8, 41)
        self._manage(bot, PremarketState(at, 2.05, bid=2.04), at)
        assert self._sells(broker) == []

    def test_the_time_stop_exits_with_a_limit(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        late = et(*self.OPEN) + dt.timedelta(minutes=CFG.bot_time_stop_minutes)
        self._manage(bot, PremarketState(late, 2.05, bid=2.04), late)
        assert bot.open_trades["PRE"]["exit"]["reason"] == "time_stop"
        assert self._sells(broker)[-1]["type"] == "limit"

    def test_scale_out_banks_with_a_limit_and_lifts_the_stop(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        at = _at(8, 42)
        self._manage(bot, PremarketState(at, 2.21, bid=2.20), at)
        trade = bot.open_trades["PRE"]
        bank = self._sells(broker)[-1]
        assert bank["type"] == "limit" and bank["extended_hours"] is True
        assert bank["qty"] == trade["bank_qty"]
        assert trade["banked"] is True
        assert trade["stop"] == pytest.approx(2.00)     # break-even

    def test_the_runner_trail_ratchets_up_and_never_down(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        at = _at(8, 42)
        self._manage(bot, PremarketState(at, 2.21, bid=2.20), at)
        trade = bot.open_trades["PRE"]
        pct = trade["trail_pct"]
        assert pct

        up = _at(8, 43)
        self._manage(bot, PremarketState(up, 2.60, bid=2.59), up)
        high_stop = trade["stop"]
        assert high_stop == pytest.approx(round(2.60 * (1 - pct / 100), 2))

        dip = _at(8, 44)
        self._manage(bot, PremarketState(dip, high_stop + 0.01), dip)
        assert trade["stop"] == pytest.approx(high_stop)   # did not follow down

        hit = _at(8, 45)
        self._manage(bot, PremarketState(hit, high_stop - 0.01), hit)
        assert trade["exit"]["reason"] == "trailing"


class TestTheBellHandsTheStopBack:
    """From 09:30 the broker takes stop orders again, and a broker-held stop
    survives this process dying. The handoff must never lower protection."""

    def _open(self, tmp_path):
        return TestPremarketStop()._open(tmp_path)

    def _manage(self, bot, state, at):
        asyncio.run(bot._manage_open(state, now=at, ts=int(at.timestamp())))

    def test_an_open_position_gets_a_broker_stop(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        bell = _at(9, 30)
        self._manage(bot, PremarketState(bell, 2.05), bell)
        stop = [o for o in broker.orders if o["type"] == "stop"][-1]
        assert stop["stop_price"] == pytest.approx(1.90)
        assert stop["qty"] == 200
        assert bot.open_trades["PRE"]["managed_stop"] is False

    def test_an_exit_still_working_is_finished_at_market(self, tmp_path):
        bot, broker, journal = self._open(tmp_path)
        at = _at(9, 29, 50)
        self._manage(bot, PremarketState(at, 1.89, bid=1.88), at)   # stop fires
        broker.closed_orders = [{"side": "sell", "filled_qty": "200",
                                 "filled_avg_price": "1.86", "legs": []}]
        bell = _at(9, 30)
        self._manage(bot, PremarketState(bell, 1.86), bell)
        assert journal.recent_trades(1)[0]["exit_reason"] == "stop"
        assert "PRE" not in bot.open_trades

    def test_a_runner_whose_trail_would_start_lower_keeps_a_fixed_stop(self, tmp_path):
        """A native trail restarts from the price at the bell. If that would
        put it under the stop the bot was holding, the higher one stays."""
        bot, broker, journal = self._open(tmp_path)
        at = _at(9, 0)
        self._manage(bot, PremarketState(at, 2.21, bid=2.20), at)   # bank
        up = _at(9, 10)
        self._manage(bot, PremarketState(up, 2.60), up)             # trail up
        held = bot.open_trades["PRE"]["stop"]

        bell = _at(9, 30)
        self._manage(bot, PremarketState(bell, held + 0.02), bell)  # dipped
        handed = [o for o in broker.orders
                  if o["type"] in ("stop", "trailing_stop")][-1]
        assert handed["type"] == "stop"
        assert handed["stop_price"] == pytest.approx(held)


def test_premarket_never_sends_an_order_alpaca_refuses_outside_hours(tmp_path):
    """The guard that replaces "premarket is never traded".

    Pre-market trading is now intended. What must never happen before 09:30
    is a market, stop, trailing or OTO order - Alpaca rejects all of them
    outside regular hours, and a rejected stop is a position with no stop.
    Walk a whole pre-market life - entry, target, trail, exit, chase - and
    check every order the bot sent.
    """
    bot, broker, journal = TestPremarketStop()._open(tmp_path)
    steps = [(_at(8, 41), 2.10), (_at(8, 42), 2.21), (_at(8, 43), 2.50),
             (_at(8, 44), 2.20), (_at(8, 44, 10), 2.19)]
    for at, px in steps:
        asyncio.run(bot._manage_open(PremarketState(at, px, bid=px - 0.01),
                                     now=at, ts=int(at.timestamp())))

    assert broker.orders, "nothing was sent - the walk did not exercise it"
    for order in broker.orders:
        assert order["type"] == "limit", order
        assert order.get("extended_hours") is True, order
        assert "order_class" not in order, order


def test_the_runner_trail_never_drops_below_break_even(tmp_path):
    """The max() in _trail_runner is what holds the floor.

    A trail taken from a high only a little above entry would sit under what
    was paid - 5% below $2.05 is $1.95 on a $2.00 entry. The runner is
    playing with the market's money; it must never be allowed to lose.
    """
    bot, _, _ = make_bot(tmp_path)
    trade = {"stop": 2.00, "trail_pct": 5.0, "high": None}
    bot._trail_runner(trade, 2.05)
    assert trade["stop"] == pytest.approx(2.00)
    bot._trail_runner(trade, 2.40)                 # high enough to lift it
    assert trade["stop"] == pytest.approx(2.28)
