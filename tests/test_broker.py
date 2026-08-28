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
