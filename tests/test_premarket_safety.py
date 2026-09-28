"""The ways a pre-market position loses its stop, sells the wrong quantity,
or is mis-sized - one test per failure the premarket-live review found.

The happy-path suite (test_bot_trading.TestPremarketStop) builds exactly one
fully filled position with a cooperative broker. Every failure here needs
something that suite never constructs: a fill that races a poll, a second
position, a broker that refuses or is slow, a partial fill, a short, or a
tape that stopped printing while the poller kept running.
"""
import asyncio
import datetime as dt
import time

import pytest

from scanner.alpaca import parse_snapshots
from scanner.config import Config
from scanner.flatten import plan_protection
from scanner.trading.bot import _position_qty
from scanner.trading.strategy import premarket_entry_limit

from .test_bot_trading import FakeState, a_pick, et, make_bot

CFG = Config()


def _at(hour, minute, second=0):
    return et(hour, minute) + dt.timedelta(seconds=second)


class _Hist:
    def __init__(self, seen, price):
        self.latest = (seen, price, 1_000_000)   # when the POLLER saw it
        self.completed_bars = []


class State:
    """Several symbols; optionally the time the tape last printed."""

    def __init__(self, at, prices, bids=None, trade_ts=None):
        self.latest = {}
        for sym, px in prices.items():
            row = {"price": px, "bid": (bids or {}).get(sym)}
            if trade_ts is not None:
                row["trade_ts"] = trade_ts
            self.latest[sym] = row
        self.histories = {sym: _Hist(at, px) for sym, px in prices.items()}


def open_premarket(tmp_path, symbols=("PRE",), qty=200):
    bot, broker, journal = make_bot(tmp_path)
    for sym in symbols:
        pick = {"symbol": sym, "price": 2.00, "premarket": True,
                "limit": 2.13, "qty": qty, "stop": 1.90, "score": 0.9,
                "setup": "micro_pullback", "features": {"rvol": 9.0}}
        asyncio.run(bot._enter(pick, ts=int(et(8, 40).timestamp())))
    broker._positions = [{"symbol": s, "qty": str(qty),
                          "avg_entry_price": "2.00", "current_price": 2.00}
                         for s in symbols]
    return bot, broker, journal


def manage(bot, state, at):
    asyncio.run(bot._manage_open(state, now=at, ts=int(at.timestamp())))


def sells(broker, symbol=None):
    return [o for o in broker.orders if o["side"] == "sell"
            and (symbol is None or o["symbol"] == symbol)]


# 1 ---------------------------------------------------------------------

class TestAFillThatBeatsThePositionRead:
    """positions() came back empty, then the entry filled, then the order
    read said "filled". With no sell behind it, that is an open position,
    not a trade that opened and closed between polls."""

    def _race(self, tmp_path):
        bot, broker, journal = make_bot(tmp_path)
        broker.order_status = "new"
        asyncio.run(bot._enter(a_pick(), ts=int(et(9, 40).timestamp())))
        broker._positions = []
        broker.order_status, broker.order_fill_price = "filled", "5.00"
        broker.closed_orders = []                # nothing was ever sold
        return bot, broker, journal

    def test_it_is_not_journalled_as_a_close_at_entry(self, tmp_path):
        bot, broker, journal = self._race(tmp_path)
        manage(bot, FakeState({}), _at(9, 40, 3))
        assert "HODX" in bot.open_trades
        assert bot.open_trades["HODX"]["filled"] is True
        assert journal.recent_trades(5) == []

    def test_the_position_is_managed_once_it_shows_up(self, tmp_path):
        bot, broker, journal = self._race(tmp_path)
        manage(bot, FakeState({}), _at(9, 40, 3))
        broker._positions = [{"symbol": "HODX", "qty": "50",
                              "avg_entry_price": "5.00", "current_price": 5.02}]
        manage(bot, FakeState({"HODX": {"price": 5.02}}), _at(9, 40, 6))
        assert "HODX" in bot.open_trades
        assert journal.recent_trades(5) == []


# 2 ---------------------------------------------------------------------

def test_a_refused_order_on_one_position_does_not_skip_the_next_stop(tmp_path):
    """One position's broker error used to abort the whole loop, so every
    position after it went a cycle - every cycle - without its stop check."""
    bot, broker, journal = open_premarket(tmp_path, symbols=("AAA", "BBB"))
    broker.refuse = {"limit": {"AAA"}}           # AAA's scale-out is refused
    at = _at(8, 42)
    manage(bot, State(at, {"AAA": 2.25, "BBB": 1.85}), at)
    assert bot.open_trades["BBB"]["exit"]["reason"] == "stop"
    assert sells(broker, "BBB")


# 3 ---------------------------------------------------------------------

class TestAFrozenTape:
    """The poller refreshes every symbol every 3 seconds whether or not IEX
    printed. Freshness has to come from the print, not from the poll."""

    def test_a_last_trade_frozen_on_the_tape_is_stale(self, tmp_path):
        bot, broker, journal = open_premarket(tmp_path)
        at = _at(8, 45)
        printed = (at - dt.timedelta(
            seconds=CFG.bot_stale_quote_seconds + 5)).timestamp()
        manage(bot, State(at, {"PRE": 2.05}, trade_ts=printed), at)
        assert bot.open_trades["PRE"]["exit"]["reason"] == "stale"

    def test_a_recent_print_is_fresh(self, tmp_path):
        bot, broker, journal = open_premarket(tmp_path)
        at = _at(8, 45)
        printed = (at - dt.timedelta(seconds=2)).timestamp()
        manage(bot, State(at, {"PRE": 2.05}, trade_ts=printed), at)
        assert sells(broker) == []

    def test_the_snapshot_keeps_when_the_trade_printed(self):
        raw = {"PRE": {
            "latestTrade": {"p": 2.00, "t": "2026-09-28T12:30:05.123456789Z"},
            "dailyBar": {"h": 2.10, "c": 2.00, "v": 1000},
            "prevDailyBar": {"c": 1.50}}}
        want = dt.datetime(2026, 9, 28, 12, 30, 5, 123456,
                           tzinfo=dt.timezone.utc).timestamp()
        assert parse_snapshots(raw)["PRE"]["trade_ts"] == pytest.approx(want)

    def test_no_trade_time_is_none_not_a_crash(self):
        raw = {"PRE": {"latestTrade": {"p": 2.00},
                       "dailyBar": {"h": 2.10, "c": 2.00, "v": 1000},
                       "prevDailyBar": {"c": 1.50}}}
        assert parse_snapshots(raw)["PRE"]["trade_ts"] is None


# 4 ---------------------------------------------------------------------

class TestTheBellNeverUnwatches:
    def test_a_price_under_the_stop_is_closed_not_handed_off(self, tmp_path):
        """A sell stop above the market is refused. Gapping through the stop
        in the seconds around the bell must close the position."""
        bot, broker, journal = open_premarket(tmp_path)
        broker.closed_orders = [{"side": "sell", "filled_qty": "200",
                                 "filled_avg_price": "1.85", "legs": []}]
        bell = _at(9, 30)
        manage(bot, State(bell, {"PRE": 1.85}), bell)
        assert not [o for o in broker.orders
                    if o["type"] in ("stop", "trailing_stop")]
        assert "PRE" not in bot.open_trades
        assert journal.recent_trades(1)[0]["exit_reason"] == "stop"

    def test_a_refused_handoff_keeps_the_stop_watched(self, tmp_path):
        bot, broker, journal = open_premarket(tmp_path)
        broker.refuse = {"stop": {"PRE"}}
        bell = _at(9, 30)
        manage(bot, State(bell, {"PRE": 2.05}), bell)     # handoff refused
        assert bot.open_trades["PRE"]["managed_stop"] is True

        later = _at(9, 30, 3)
        manage(bot, State(later, {"PRE": 1.85}), later)
        assert "PRE" not in bot.open_trades              # closed at market


# 5 ---------------------------------------------------------------------

def _pos(qty="200", price="2.05", avg="2.00", sym="PRE"):
    return {"symbol": sym, "qty": qty, "current_price": price,
            "avg_entry_price": avg}


def _sell(kind="stop", qty="200", sym="PRE", oid="s1"):
    return {"id": oid, "symbol": sym, "side": "sell", "type": kind,
            "qty": qty}


class TestProtectingPositionsTheBotLeftBehind:
    """If the session dies holding a pre-market position, nothing stops it:
    the stop was the process. After the bell the flatten job attaches one."""

    def test_a_broker_stop_already_there_is_left_alone(self):
        plan = plan_protection([_pos()], [_sell()], {"PRE": 1.90}, CFG)
        assert plan[0]["action"] == "ok"

    def test_a_trailing_stop_counts_as_protection(self):
        plan = plan_protection([_pos()], [_sell("trailing_stop")],
                               {"PRE": 1.90}, CFG)
        assert plan[0]["action"] == "ok"

    def test_an_unprotected_position_gets_the_journals_stop(self):
        plan = plan_protection([_pos()], [], {"PRE": 1.90}, CFG)
        assert plan[0] == {"symbol": "PRE", "action": "stop", "qty": 200,
                           "stop": 1.90, "cancel": []}

    def test_a_resting_limit_is_not_protection_and_is_cleared_first(self):
        plan = plan_protection([_pos()], [_sell("limit", oid="lim")],
                               {"PRE": 1.90}, CFG)
        assert plan[0]["action"] == "stop" and plan[0]["qty"] == 200
        assert plan[0]["cancel"] == ["lim"]

    def test_only_the_uncovered_part_gets_a_new_stop(self):
        plan = plan_protection([_pos()], [_sell(qty="70")], {"PRE": 1.90},
                               CFG)
        assert plan[0]["action"] == "stop" and plan[0]["qty"] == 130

    def test_already_through_the_stop_is_closed(self):
        plan = plan_protection([_pos(price="1.88")], [], {"PRE": 1.90}, CFG)
        assert plan[0]["action"] == "close"

    def test_no_journal_row_falls_back_to_the_configured_stop(self):
        plan = plan_protection([_pos()], [], {}, CFG)
        assert plan[0]["stop"] == pytest.approx(
            round(2.00 * (1 - CFG.bot_stop_pct / 100), 2))

    def test_a_short_is_reported_never_sold(self):
        plan = plan_protection([_pos(qty="-50")], [], {"PRE": 1.90}, CFG)
        assert plan[0]["action"] == "short"


class TestTheProtectRun:
    def test_after_the_bell_it_places_the_missing_stop(self, tmp_path):
        from scanner.flatten import protect
        bot, broker, journal = open_premarket(tmp_path)   # journal stop 1.90
        broker._positions = [_pos()]
        asyncio.run(protect(broker, journal, CFG, now=_at(9, 31)))
        stop = [o for o in broker.orders if o["type"] == "stop"][-1]
        assert stop["qty"] == 200
        assert stop["stop_price"] == pytest.approx(1.90)

    def test_before_the_bell_it_tries_to_get_out_first(self, tmp_path,
                                                      monkeypatch):
        import scanner.flatten as flatten
        bot, broker, journal = open_premarket(tmp_path)
        broker._positions = [_pos()]

        async def no_wait(_):
            return None
        monkeypatch.setattr(flatten.asyncio, "sleep", no_wait)
        asyncio.run(flatten.protect(broker, journal, CFG, now=_at(8, 50)))
        exit_sell = [o for o in sells(broker) if o["type"] == "limit"][0]
        assert exit_sell["extended_hours"] is True
        assert exit_sell["qty"] == 200
        assert [o for o in broker.orders if o["type"] == "stop"]  # then guarded


def test_a_dead_bot_ends_the_session_early(monkeypatch):
    """The protect step runs when the session step ends. A session that
    kept the scanner alive for hours after the bot died would hold that
    step back until 12:45."""
    import scanner.session as session

    async def forever(*_):
        await asyncio.sleep(3600)

    async def dies(*_):
        raise RuntimeError("bot crashed")

    monkeypatch.setattr(session, "live_loop", forever)
    monkeypatch.setattr(session, "status_writer", forever)
    monkeypatch.setattr(session, "bot_loop", dies)
    started = time.monotonic()
    survived = asyncio.run(session.run_for(CFG, seconds=60, with_bot=True))
    assert survived is False
    assert time.monotonic() - started < 5


# 6 ---------------------------------------------------------------------

class TestAPartialFillIsSoldAsHeld:
    def test_premarket_scale_out_banks_only_what_was_bought(self, tmp_path):
        bot, broker, journal = open_premarket(tmp_path)          # asked 200
        broker._positions = [_pos(qty="80", price="2.21")]      # got 80
        at = _at(8, 42)
        manage(bot, State(at, {"PRE": 2.21}, bids={"PRE": 2.20}), at)
        bank = sells(broker)[-1]
        trade = bot.open_trades["PRE"]
        assert bank["qty"] + trade["runner_qty"] == 80
        assert 1 <= bank["qty"] < 80

    def test_the_journal_pays_on_what_was_bought(self, tmp_path):
        """P&L is (exit - entry) x the journal's quantity. Left at the 200
        ordered, an 80-share fill reported two and a half times its result."""
        bot, broker, journal = open_premarket(tmp_path)
        broker._positions = [_pos(qty="80", price="2.21")]
        at = _at(8, 42)
        manage(bot, State(at, {"PRE": 2.21}, bids={"PRE": 2.20}), at)
        assert journal.trades_today("2026-07-14")[0]["qty"] == 80

    def test_regular_scale_out_banks_only_what_was_bought(self, tmp_path):
        bot, broker, journal = make_bot(tmp_path)
        asyncio.run(bot._enter(a_pick(qty=50), ts=int(et(9, 40).timestamp())))
        broker._positions = [{"symbol": "HODX", "qty": "20",
                              "avg_entry_price": "5.00", "current_price": 5.21}]
        at = et(9, 42)
        manage(bot, FakeState({"HODX": {"price": 5.21}}), at)
        sold = sum(o["qty"] for o in sells(broker, "HODX"))
        assert sold == 20                     # bank + runner, nothing more

    def test_a_failed_re_read_still_sells_in_regular_hours(self, tmp_path):
        """The broker stop was already cancelled. Aborting on a failed
        position read would leave the position with no stop and no sale."""
        bot, broker, journal = make_bot(tmp_path)
        asyncio.run(bot._enter(a_pick(qty=50), ts=int(et(9, 40).timestamp())))
        broker._positions = [{"symbol": "HODX", "qty": "50",
                              "avg_entry_price": "5.00", "current_price": 5.21}]
        reads = []
        real = broker.positions

        async def second_read_fails():
            reads.append(1)
            if len(reads) > 1:
                raise RuntimeError("positions timed out")
            return await real()
        broker.positions = second_read_fails
        manage(bot, FakeState({"HODX": {"price": 5.21}}), et(9, 42))
        assert sum(o["qty"] for o in sells(broker, "HODX")) == 50
        assert bot.open_trades["HODX"]["banked"] is True


# 7 ---------------------------------------------------------------------

class TestAShortIsNeverAddedTo:
    def test_the_held_quantity_of_a_short_is_zero(self):
        trade = {"qty": 200, "runner_qty": 70, "banked": False}
        assert _position_qty({"qty": "-50"}, trade) == 0

    def test_a_stop_on_a_short_sells_nothing(self, tmp_path, capsys):
        bot, broker, journal = open_premarket(tmp_path)
        broker._positions = [_pos(qty="-50", price="1.85")]
        at = _at(8, 41)
        manage(bot, State(at, {"PRE": 1.85}, bids={"PRE": 1.84}), at)
        assert sells(broker) == []
        assert "SHORT" in capsys.readouterr().out


# 8 ---------------------------------------------------------------------

class TestASellWaitsForItsCancel:
    """Alpaca reserves shares for an order until its cancel completes, so a
    sell sent straight after a cancel can be refused for quantity."""

    def test_the_stop_is_not_sent_until_the_cancel_settles(self, tmp_path):
        bot, broker, journal = open_premarket(tmp_path)
        broker.slow_cancels = {"PRE"}
        at = _at(8, 41)
        manage(bot, State(at, {"PRE": 1.85}, bids={"PRE": 1.84}), at)
        assert sells(broker) == []
        assert not bot.open_trades["PRE"].get("exit")

        broker.slow_cancels = set()
        later = _at(8, 41, 3)
        manage(bot, State(later, {"PRE": 1.85}, bids={"PRE": 1.84}), later)
        assert sells(broker)[-1]["qty"] == 200
        assert bot.open_trades["PRE"]["exit"]["reason"] == "stop"

    def test_a_chase_waits_for_the_cancel_too(self, tmp_path):
        bot, broker, journal = open_premarket(tmp_path)
        at = _at(8, 41)
        manage(bot, State(at, {"PRE": 1.85}, bids={"PRE": 1.84}), at)
        first = bot.open_trades["PRE"]["exit"]["px"]
        n = len(sells(broker))

        broker.slow_cancels = {"PRE"}
        later = at + dt.timedelta(seconds=CFG.bot_premarket_chase_seconds)
        manage(bot, State(later, {"PRE": 1.85}, bids={"PRE": 1.84}), later)
        assert len(sells(broker)) == n
        assert bot.open_trades["PRE"]["exit"]["px"] == pytest.approx(first)


# 9 ---------------------------------------------------------------------

class TestTheAskIsOnlyTrustedNearTheLastTrade:
    """On the free feed the ask is IEX's own book: often one-sided, stale,
    or far from where the stock trades."""

    def test_an_ask_far_above_the_last_trade_is_ignored(self):
        assert premarket_entry_limit(2.00, 2.50, CFG) == pytest.approx(2.10)

    def test_a_stale_ask_below_the_stop_is_ignored(self):
        assert premarket_entry_limit(2.00, 1.80, CFG) == pytest.approx(2.10)

    def test_an_ask_near_the_last_trade_is_used(self):
        assert premarket_entry_limit(2.00, 2.03, CFG) == pytest.approx(2.13)
