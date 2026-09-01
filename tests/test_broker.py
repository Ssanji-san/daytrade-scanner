from dataclasses import replace

import pytest

from scanner.config import Config
from scanner.trading.broker import Broker, PaperOnlyError

CFG = Config()


def test_refuses_non_paper_endpoint():
    live = replace(CFG, trading_base="https://api.alpaca.markets")
    with pytest.raises(PaperOnlyError):
        Broker(session=None, cfg=live)


def test_accepts_paper_endpoint():
    broker = Broker(session=None, cfg=CFG)
    assert "paper-api" in broker.base


def test_market_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.market_payload("HODX", qty=50, side="buy") == {
        "symbol": "HODX", "qty": "50", "side": "buy",
        "type": "market", "time_in_force": "day",
    }


def test_stop_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.stop_payload("HODX", qty=25, stop_price=4.85) == {
        "symbol": "HODX", "qty": "25", "side": "sell",
        "type": "stop", "time_in_force": "day", "stop_price": "4.85",
    }


def test_oto_stop_payload_is_one_complex_order():
    """Entry and stop must travel together or Alpaca calls it a wash trade."""
    broker = Broker(session=None, cfg=CFG)
    assert broker.oto_stop_payload("HODX", qty=50, stop_price=4.85) == {
        "symbol": "HODX", "qty": "50", "side": "buy",
        "type": "market", "time_in_force": "day",
        "order_class": "oto",
        "stop_loss": {"stop_price": "4.85"},
    }


def test_trailing_stop_payload():
    broker = Broker(session=None, cfg=CFG)
    assert broker.trailing_stop_payload("HODX", qty=25, trail_percent=5.0) == {
        "symbol": "HODX", "qty": "25", "side": "sell",
        "type": "trailing_stop", "time_in_force": "day", "trail_percent": "5",
    }


class TestSellLegs:
    """Exit fills must come from the trade being closed, not the symbol.

    /v2/orders answers newest-first across the whole account history, so an
    unbounded query averaged a previous session's exits on the same symbol
    into today's R multiple.
    """

    ORDERS = [
        {"side": "sell", "filled_qty": "10", "filled_avg_price": "5.00",
         "filled_at": "2026-07-14T14:00:00Z", "legs": []},
        {"side": "sell", "filled_qty": "10", "filled_avg_price": "9.00",
         "filled_at": "2026-07-01T14:00:00Z", "legs": []},   # a prior session
    ]

    def _ts(self, iso):
        import datetime as dt
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    def test_only_fills_after_the_open_time_count(self):
        legs = Broker.sell_legs(self.ORDERS,
                                self._ts("2026-07-14T13:30:00Z"))
        assert legs == [(10.0, 5.00)]

    def test_an_unfilled_leg_is_ignored(self):
        orders = [{"side": "sell", "filled_qty": "0",
                   "filled_avg_price": None, "legs": []}]
        assert Broker.sell_legs(orders, 0) == []

    def test_a_leg_with_no_fill_time_is_kept(self):
        """Alpaca omits filled_at on some legs; dropping them lost real exits."""
        orders = [{"side": "sell", "filled_qty": "5",
                   "filled_avg_price": "4.00", "legs": []}]
        assert Broker.sell_legs(orders, 1_800_000_000) == [(5.0, 4.00)]

    def test_attached_legs_are_read_too(self):
        orders = [{"side": "buy", "filled_qty": "10", "legs": [
            {"side": "sell", "filled_qty": "10", "filled_avg_price": "5.50",
             "filled_at": "2026-07-14T14:05:00Z"}]}]
        legs = Broker.sell_legs(orders, self._ts("2026-07-14T13:30:00Z"))
        assert legs == [(10.0, 5.50)]
